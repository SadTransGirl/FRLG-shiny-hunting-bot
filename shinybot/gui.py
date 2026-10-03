"""Window: live capture preview, clickable controller, keyboard control, hunt buttons.

Keyboard input only reaches the Switch while this window is focused; everything
is released as soon as it loses focus.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
import cv2

from . import theme
from .app import OUTPUT_DIR, alert, open_controller, run_test_sequence, setup_logging
from .audio import AudioPassthrough, guess_capture_device, list_inputs
from .capture import FrameGrabber
from .config import Config
from .editor import SequenceEditor
from .hunt import SETUP_FRAME, Hunter, HuntStopped
from .keymap import InputState, apply_to_controller
from .recorder import Recorder

logger = logging.getLogger('shinybot.gui')

PREVIEW_WIDTH = 640  # before display scaling
SOFT_RESET = ('A', 'B', 'PLUS', 'MINUS')
# Ignore a key release if the same key is pressed again within this many ms
# (X11 sends release/press pairs for auto-repeat).
RELEASE_DEBOUNCE_MS = 40

# (label, button, row, column, uses symbol font) of the on-screen controller
PAD_LAYOUT = [
    ('ZL', 'ZL', 0, 0, False), ('L', 'L', 0, 1, False), ('R', 'R', 0, 4, False), ('ZR', 'ZR', 0, 5, False),
    ('-', 'MINUS', 1, 1, False), ('Capture', 'CAPTURE', 1, 2, False), ('Home', 'HOME', 1, 3, False),
    ('+', 'PLUS', 1, 4, False),
    ('▲', 'UP', 2, 1, True), ('X', 'X', 2, 4, False),
    ('◀', 'LEFT', 3, 0, True), ('▶', 'RIGHT', 3, 2, True), ('Y', 'Y', 3, 3, False), ('A', 'A', 3, 5, False),
    ('▼', 'DOWN', 4, 1, True), ('B', 'B', 4, 4, False),
]
PAGES = (('hunt', 'Hunt'), ('play', 'Play'), ('setup', 'Setup'))


class AsyncWorker:
    """Runs the asyncio loop (Bluetooth, hunting) in a background thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn, *args) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


class _UiLogHandler(logging.Handler):
    def __init__(self, app: App) -> None:
        super().__init__(logging.INFO)
        self.app = app
        self.setFormatter(logging.Formatter('%(asctime)s  %(message)s', '%H:%M:%S'))
        self.addFilter(lambda r: r.name.startswith(('shinybot', 'bt_controller')))

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        self.app.post(lambda: self.app.append_log(message))


# ---- themed building blocks ----------------------------------------------------

def F(name: str):
    return theme.F[name]


class Card(ctk.CTkFrame):
    """Rounded panel with an optional heading; put content in .body."""

    def __init__(self, parent, title: str | None = None, hint: str | None = None) -> None:
        super().__init__(parent, fg_color=theme.PANEL, corner_radius=theme.RADIUS)
        if title:
            ctk.CTkLabel(self, text=title, font=F('heading'), text_color=theme.TEXT,
                         anchor='w').pack(fill='x', padx=16, pady=(10, 0))
        if hint:
            ctk.CTkLabel(self, text=hint, font=F('small'), text_color=theme.TEXT_MUTED, anchor='w',
                         justify='left', wraplength=520).pack(fill='x', padx=16)
        self.body = ctk.CTkFrame(self, fg_color='transparent')
        self.body.pack(fill='both', expand=True, padx=16, pady=(8, 14))


class Btn(ctk.CTkButton):
    def __init__(self, parent, text: str, command=None, kind: str = 'normal', width: int = 120,
                 height: int = 36, **kw) -> None:
        super().__init__(parent, text=text, command=command, width=width, height=height,
                         corner_radius=10, border_width=0, font=kw.pop('font', F('button')), **kw)
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        colors = {
            'primary': (theme.ACCENT, theme.ACCENT_HOVER, theme.ACCENT_TEXT),
            'danger': (theme.DANGER, '#f58a97', '#2a0b10'),
            'normal': (theme.PANEL_ALT, theme.PANEL_HOVER, theme.TEXT),
        }[kind]
        self.configure(fg_color=colors[0], hover_color=colors[1], text_color=colors[2])


def label(parent, text: str = '', muted: bool = False, font: str = 'body', **kw) -> ctk.CTkLabel:
    return ctk.CTkLabel(parent, text=text, font=F(font), anchor='w', justify='left',
                        text_color=theme.TEXT_MUTED if muted else theme.TEXT, **kw)


def option(parent, var: tk.StringVar, values: list[str], command=None, width: int = 260):
    return ctk.CTkOptionMenu(parent, variable=var, values=values or [''], width=width, height=34,
                             corner_radius=8, fg_color=theme.PANEL_ALT, button_color=theme.PANEL_HOVER,
                             button_hover_color=theme.BORDER, text_color=theme.TEXT,
                             dropdown_fg_color=theme.PANEL_ALT, dropdown_hover_color=theme.ACCENT_DIM,
                             dropdown_text_color=theme.TEXT, font=F('body'), dropdown_font=F('body'),
                             dynamic_resizing=False, command=command)


def pill(parent, text: str, command=None) -> ctk.CTkButton:
    return ctk.CTkButton(parent, text=text, font=F('small'), height=28, corner_radius=14, width=10,
                         fg_color=theme.PANEL_ALT, hover_color=theme.PANEL_HOVER,
                         text_color=theme.TEXT_MUTED, command=command)


class Video(ctk.CTkFrame):
    """The capture card picture, on a canvas so boxes can be drawn over it."""

    def __init__(self, parent, width: int) -> None:
        super().__init__(parent, fg_color=theme.SIDEBAR, corner_radius=10)
        self.canvas = tk.Canvas(self, width=width, height=width * 9 // 16, bg=theme.SIDEBAR,
                                highlightthickness=0, bd=0)
        self.canvas.pack(padx=6, pady=6)
        self.image_item = self.canvas.create_image(0, 0, anchor='nw')
        self.text_item = self.canvas.create_text(width // 2, width * 9 // 32, fill=theme.TEXT_MUTED,
                                                 text='no camera', font=F('body'))


class App:
    NO_AUDIO = '(off)'

    def __init__(self, root: ctk.CTk, config: Config) -> None:
        self.root = root
        self.config = config
        self.worker = AsyncWorker()
        self.controller = None
        self.frames: FrameGrabber | None = None
        self.hunter: Hunter | None = None
        self.job: tuple[str, object] | None = None  # (name, concurrent future)
        self.input = InputState(config.keys)
        self._pending_release: dict[str, str] = {}
        self._ui_queue: queue.Queue = queue.Queue()
        self._frame = None
        self._frame_time = 0.0
        self._scale = 1.0
        self._photo = None
        self._box_mode: str | None = None
        self._drag_start: tuple[int, int] | None = None
        self._warned_no_input = False
        self.recorder: Recorder | None = None
        self._warned_stick_recording = False
        self.audio = AudioPassthrough()
        self.audio.muted = config.audio_muted
        self.audio.volume = config.audio_volume
        self._audio_devices = []
        self.active_page = 'hunt'

        theme.init(root)
        # The canvas isn't scaled by CustomTkinter, so follow Windows display scaling here.
        self.preview_width = int(PREVIEW_WIDTH * ctk.ScalingTracker.get_window_scaling(root))
        self._build()
        logging.getLogger().addHandler(_UiLogHandler(self))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind_all('<KeyPress>', self._on_key_press)
        root.bind_all('<KeyRelease>', self._on_key_release)
        root.bind('<FocusOut>', lambda _e: root.after(50, self._check_focus), add='+')
        self._tick_id = root.after(30, self._tick)
        self.open_camera()
        self._init_audio()

    # -- layout ---------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        top = ctk.CTkFrame(root, fg_color=theme.SIDEBAR, corner_radius=0, height=60)
        top.pack(fill='x')
        top.pack_propagate(False)
        ctk.CTkLabel(top, text='', image=theme.icon('app', 38)).pack(side='left', padx=(16, 10))
        ctk.CTkLabel(top, text='Shiny Bot', font=F('title'), text_color=theme.TEXT).pack(side='left')
        ctk.CTkLabel(top, text='FireRed / LeafGreen', font=F('small'), text_color=theme.ACCENT,
                     fg_color=theme.ACCENT_DIM, corner_radius=10, height=26, padx=10).pack(
            side='left', padx=12)
        self.sound_pill = pill(top, 'Sound', command=self.toggle_mute)
        self.sound_pill.pack(side='right', padx=(0, 16))
        self.camera_pill = pill(top, 'Camera', command=lambda: self.select_page('setup'))
        self.camera_pill.pack(side='right', padx=(0, 10))
        self.controller_pill = pill(top, 'Controller', command=lambda: self.select_page('setup'))
        self.controller_pill.pack(side='right', padx=(0, 10))

        body = ctk.CTkFrame(root, fg_color=theme.BG, corner_radius=0)
        body.pack(fill='both', expand=True)
        nav = ctk.CTkFrame(body, fg_color=theme.SIDEBAR, corner_radius=0, width=104)
        nav.pack(side='left', fill='y')
        nav.pack_propagate(False)
        content = ctk.CTkFrame(body, fg_color=theme.BG, corner_radius=0)
        content.pack(side='left', fill='both', expand=True, padx=16, pady=14)
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self.pages = {}
        self.nav_buttons = {}
        for key, text in PAGES:
            page = ctk.CTkFrame(content, fg_color=theme.BG, corner_radius=0)
            page.grid(row=0, column=0, sticky='nsew')
            self.pages[key] = page
            button = ctk.CTkButton(nav, text=text, compound='top', width=88, height=80, corner_radius=12,
                                   image=theme.icon(key, 30), fg_color='transparent',
                                   hover_color=theme.PANEL, text_color=theme.TEXT_MUTED, font=F('nav'),
                                   command=lambda k=key: self.select_page(k))
            button.pack(side='top', pady=(12 if key == 'hunt' else 4, 0))
            self.nav_buttons[key] = button

        self._build_hunt(self.pages['hunt'])
        self._build_play(self.pages['play'])
        self._build_setup(self.pages['setup'])
        self.select_page('hunt')

    def _page_header(self, page, title: str, subtitle: str) -> None:
        header = ctk.CTkFrame(page, fg_color=theme.PANEL, corner_radius=theme.RADIUS)
        header.pack(fill='x', pady=(0, 12))
        ctk.CTkLabel(header, text=title, font=F('title'), text_color=theme.TEXT, anchor='w').pack(
            fill='x', padx=18, pady=(8, 0))
        ctk.CTkLabel(header, text=subtitle, font=F('small'), text_color=theme.ACCENT, anchor='w').pack(
            fill='x', padx=18, pady=(0, 6))
        ctk.CTkFrame(header, fg_color=theme.ACCENT, height=3, corner_radius=2).pack(
            fill='x', padx=12, pady=(0, 6))

    def _build_hunt(self, page) -> None:
        self._page_header(page, 'Hunt', 'soft reset, summary screen, shiny check, repeat')
        row = ctk.CTkFrame(page, fg_color='transparent')
        row.pack(fill='x')
        video_card = Card(row, 'Capture', 'Drag on the video after "Mark sprite/screen box".')
        video_card.pack(side='left', fill='y', padx=(0, 12))
        self.hunt_video = Video(video_card.body, self.preview_width)
        self.hunt_video.pack()
        self.canvas = self.hunt_video.canvas
        self.canvas.bind('<ButtonPress-1>', self._drag_begin)
        self.canvas.bind('<B1-Motion>', self._drag_move)
        self.canvas.bind('<ButtonRelease-1>', self._drag_end)

        side = ctk.CTkFrame(row, fg_color='transparent')
        side.pack(side='left', fill='both', expand=True)
        hunt = Card(side, 'Shiny hunt')
        hunt.pack(fill='x')
        stats = ctk.CTkFrame(hunt.body, fg_color=theme.PANEL_ALT, corner_radius=10)
        stats.pack(fill='x', pady=(0, 10))
        label(stats, 'Resets', muted=True, font='small').pack(anchor='w', padx=14, pady=(8, 0))
        self.resets_label = ctk.CTkLabel(stats, text='0', font=F('big'), text_color=theme.ACCENT,
                                         anchor='w')
        self.resets_label.pack(anchor='w', padx=14)
        self.hunt_status = label(stats, 'not hunting', muted=True, font='small')
        self.hunt_status.pack(anchor='w', padx=14, pady=(0, 8))
        buttons = ctk.CTkFrame(hunt.body, fg_color='transparent')
        buttons.pack(fill='x')
        Btn(buttons, 'Test sequence', self.test_sequence, width=150).pack(side='left', padx=(0, 6))
        Btn(buttons, 'Start hunt', self.start_hunt, kind='primary', width=130).pack(side='left', padx=(0, 6))
        Btn(buttons, 'Stop', self.stop_job, kind='danger', width=90).pack(side='left')

        sequence = Card(side, 'Sequence', 'The button presses for one reset.')
        sequence.pack(fill='x', pady=(12, 0))
        self.sequence_var = tk.StringVar(value=self.config.active_sequence)
        self.sequence_box = option(sequence.body, self.sequence_var, list(self.config.sequences),
                                   command=lambda _v: self._choose_sequence())
        self.sequence_box.pack(fill='x', pady=(0, 8))
        buttons = ctk.CTkFrame(sequence.body, fg_color='transparent')
        buttons.pack(fill='x')
        self.record_button = Btn(buttons, 'Record', self.toggle_record, width=150)
        self.record_button.pack(side='left', padx=(0, 6))
        Btn(buttons, 'Edit', self.edit_sequence, width=100).pack(side='left')

        regions = Card(side, 'Regions', 'With the summary screen showing.')
        regions.pack(fill='x', pady=(12, 0))
        buttons = ctk.CTkFrame(regions.body, fg_color='transparent')
        buttons.pack(fill='x')
        Btn(buttons, 'Mark sprite box', lambda: self.mark_box('sprite'), width=170).pack(
            side='left', padx=(0, 6))
        Btn(buttons, 'Mark screen box', lambda: self.mark_box('screen'), width=170).pack(side='left')

        log_card = Card(page, 'Log')
        log_card.pack(fill='both', expand=True, pady=(12, 0))
        self.log = ctk.CTkTextbox(log_card.body, font=F('mono'), fg_color=theme.SIDEBAR,
                                  text_color=theme.TEXT, corner_radius=10, wrap='word', height=150)
        self.log.pack(fill='both', expand=True)
        self.log.configure(state='disabled')

    def _build_play(self, page) -> None:
        self._page_header(page, 'Play', 'control the Switch from this window')
        row = ctk.CTkFrame(page, fg_color='transparent')
        row.pack(fill='both', expand=True)
        video_card = Card(row, 'Capture')
        video_card.pack(side='left', fill='y', padx=(0, 12))
        self.play_video = Video(video_card.body, self.preview_width)
        self.play_video.pack()

        side = ctk.CTkFrame(row, fg_color='transparent')
        side.pack(side='left', fill='both', expand=True)
        pad_card = Card(side, 'Controller', 'Click and hold, or use the keyboard.')
        pad_card.pack(fill='x')
        pad = ctk.CTkFrame(pad_card.body, fg_color='transparent')
        pad.pack()
        self.pad_buttons: dict[str, ctk.CTkButton] = {}
        for text, button, row_index, column, symbol in PAD_LAYOUT:
            widget = ctk.CTkButton(pad, text=text, width=64 if len(text) < 3 else 84, height=40,
                                   corner_radius=10, font=F('symbol') if symbol else F('body'),
                                   fg_color=theme.PANEL_ALT, hover_color=theme.PANEL_HOVER,
                                   text_color=theme.TEXT)
            widget.grid(row=row_index, column=column, padx=3, pady=3)
            widget.bind('<ButtonPress-1>', lambda _e, b=button: self._pad(b, True), add='+')
            widget.bind('<ButtonRelease-1>', lambda _e, b=button: self._pad(b, False), add='+')
            self.pad_buttons[button] = widget
        Btn(pad_card.body, 'Soft reset  (A+B+Start+Select)', self.soft_reset, height=36).pack(
            fill='x', pady=(10, 0))

        keys_card = Card(side, 'Keyboard', 'Works while this window is focused.')
        keys_card.pack(fill='x', pady=(12, 0))
        names = {'return': 'Enter', 'backspace': 'Bksp'}
        entries = list(self.config.keys.items())
        rows = (len(entries) + 2) // 3
        for index, (key, target) in enumerate(entries):
            row_frame = ctk.CTkFrame(keys_card.body, fg_color='transparent')
            row_frame.grid(row=index % rows, column=index // rows, sticky='w', padx=(0, 18))
            ctk.CTkLabel(row_frame, text=names.get(key, key.capitalize()), font=F('small'), width=60,
                         fg_color=theme.PANEL_ALT, corner_radius=6, text_color=theme.ACCENT).pack(
                side='left', pady=1)
            label(row_frame, target.replace('_', ' ').replace('LS', 'Stick'), font='small').pack(
                side='left', padx=8)

    def _build_setup(self, page) -> None:
        self._page_header(page, 'Setup', 'controller, capture card and sound')
        grid = ctk.CTkFrame(page, fg_color='transparent')
        grid.pack(fill='both', expand=True)
        grid.grid_columnconfigure((0, 1), weight=1, uniform='setup')

        controller = Card(grid, 'Controller', 'The USB Bluetooth adapter pretends to be a Pro '
                                              'Controller. Pair once, then Connect each session.')
        controller.grid(row=0, column=0, sticky='nsew', padx=(0, 6), pady=(0, 12))
        self.status = label(controller.body, 'not connected', muted=True)
        self.status.pack(fill='x', pady=(0, 8))
        buttons = ctk.CTkFrame(controller.body, fg_color='transparent')
        buttons.pack(fill='x')
        Btn(buttons, 'Connect', self.connect, kind='primary').pack(side='left', padx=(0, 6))
        Btn(buttons, 'Pair', self.pair).pack(side='left')

        camera = Card(grid, 'Capture card', 'If OBS has the card, close it or use its Virtual Camera.')
        camera.grid(row=0, column=1, sticky='nsew', padx=(6, 0), pady=(0, 12))
        row = ctk.CTkFrame(camera.body, fg_color='transparent')
        row.pack(fill='x')
        label(row, 'Camera').pack(side='left', padx=(0, 8))
        self.camera_var = tk.StringVar(value=str(self.config.camera))
        option(row, self.camera_var, [str(i) for i in range(10)], width=80).pack(side='left')
        Btn(row, 'Open', self.open_camera, width=90).pack(side='left', padx=8)

        audio = Card(grid, 'Sound', 'Plays the capture card through your speakers. '
                                    'Muting only affects this PC.')
        audio.grid(row=1, column=0, sticky='nsew', padx=(0, 6), pady=(0, 12))
        self.audio_var = tk.StringVar(value=self.NO_AUDIO)
        self.audio_box = option(audio.body, self.audio_var, [self.NO_AUDIO],
                                command=lambda _v: self._choose_audio(), width=380)
        self.audio_box.pack(fill='x', pady=(0, 10))
        row = ctk.CTkFrame(audio.body, fg_color='transparent')
        row.pack(fill='x')
        self.mute_var = tk.BooleanVar(value=self.audio.muted)
        self.mute_switch = ctk.CTkSwitch(row, text='Mute', variable=self.mute_var, onvalue=True,
                                         offvalue=False, command=self._mute_switched, font=F('body'),
                                         text_color=theme.TEXT, progress_color=theme.ACCENT,
                                         fg_color=theme.BORDER, button_color='#dfe6f2',
                                         button_hover_color='#ffffff', switch_width=40, switch_height=20)
        self.mute_switch.pack(side='left', padx=(0, 16))
        label(row, 'Volume', muted=True).pack(side='left', padx=(0, 8))
        self.volume_var = tk.DoubleVar(value=self.config.audio_volume * 100)
        volume = ctk.CTkSlider(row, from_=0, to=100, variable=self.volume_var, width=160,
                               command=lambda _v: self._set_volume(), progress_color=theme.ACCENT,
                               button_color=theme.ACCENT, button_hover_color=theme.ACCENT_HOVER,
                               fg_color=theme.PANEL_ALT)
        volume.pack(side='left')
        volume.bind('<ButtonRelease-1>', lambda _e: self.config.save(), add='+')

        about = Card(grid, 'About')
        about.grid(row=1, column=1, sticky='nsew', padx=(6, 0), pady=(0, 12))
        label(about.body, 'Font: "Pokemon Pixel Font" by SpyroSteak (CC BY-SA).', muted=True,
              font='small', wraplength=420).pack(fill='x')
        label(about.body, 'Settings, screenshots and logs:', muted=True, font='small').pack(
            fill='x', pady=(8, 2))
        label(about.body, 'shinybot.json\nshinybot_output\\', muted=True, font='mono').pack(fill='x')

    def select_page(self, key: str) -> None:
        self.pages[key].tkraise()
        self.active_page = key
        for name, button in self.nav_buttons.items():
            on = name == key
            button.configure(fg_color=theme.ACCENT_DIM if on else 'transparent',
                             text_color=theme.ACCENT if on else theme.TEXT_MUTED,
                             image=theme.icon(name, 30, theme.ACCENT if on else theme.TEXT_MUTED))
        self._frame_time = 0.0  # redraw the preview on the newly shown page

    # -- thread plumbing --------------------------------------------------------

    def post(self, fn) -> None:
        """Run `fn` on the Tk thread (safe to call from any thread)."""
        self._ui_queue.put(fn)

    def append_log(self, message: str) -> None:
        self.log.configure(state='normal')
        self.log.insert('end', message + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _tick(self) -> None:
        while not self._ui_queue.empty():
            self._ui_queue.get_nowait()()
        self._update_preview()
        self._update_status()
        self._tick_id = self.root.after(30, self._tick)

    # -- controller -----------------------------------------------------------

    def _busy(self) -> str | None:
        return self.job[0] if self.job else None

    @staticmethod
    def _set_pill(widget, text: str, color: str, background: str = theme.PANEL_ALT) -> None:
        if widget.cget('text') != text or widget.cget('text_color') != color:
            widget.configure(text=text, text_color=color, fg_color=background)

    def _update_status(self) -> None:
        busy = self._busy()
        if busy == 'pair':
            text, color = 'waiting: open Controllers > Change Grip/Order', theme.WARNING
        elif busy == 'connect':
            text, color = 'connecting', theme.WARNING
        elif self.controller and self.controller.connected:
            text, color = 'connected', theme.SUCCESS
        else:
            text, color = 'not connected', theme.TEXT_MUTED
        if busy in ('hunt', 'test'):
            text += ' (bot is in control)'
        if self.status.cget('text') != text:
            self.status.configure(text=text, text_color=color)
        self._set_pill(self.controller_pill, 'Controller', color)
        self._set_pill(self.camera_pill, 'Camera', theme.SUCCESS if self.frames else theme.TEXT_MUTED)
        if self.audio.device is None:
            self._set_pill(self.sound_pill, 'Sound off', theme.TEXT_MUTED)
        elif self.audio.muted:
            self._set_pill(self.sound_pill, 'Muted', '#2a0b10', theme.DANGER)
        else:
            self._set_pill(self.sound_pill, 'Sound', theme.SUCCESS)
        if self.hunter:
            last = self.hunter.last_result
            resets = str(self.hunter.stats.resets)
            if self.resets_label.cget('text') != resets:
                self.resets_label.configure(text=resets)
            state = 'hunting' if busy == 'hunt' else 'stopped'
            status = f'{state}  -  last: {last.verdict}' if last else state
            if self.hunt_status.cget('text') != status:
                self.hunt_status.configure(text=status)

    async def _ensure_controller(self):
        if self.controller is None:
            self.controller = await open_controller(self.config)
        return self.controller

    def connect(self) -> None:
        async def run():
            controller = await self._ensure_controller()
            if not controller.connected:
                await controller.reconnect()
        self._start_job('connect', run)

    def pair(self) -> None:
        if not messagebox.askokcancel(
                'Pair', 'On the Switch open Controllers > Change Grip/Order, then press OK.'):
            return

        async def run():
            controller = await self._ensure_controller()
            await controller.pair()
        self._start_job('pair', run)

    def _manual_allowed(self) -> bool:
        return (self.controller is not None and self.controller.connected
                and self._busy() not in ('hunt', 'test'))

    def _apply_input(self) -> None:
        buttons, stick = self.input.buttons(), self.input.stick()
        for name, widget in self.pad_buttons.items():
            held = name in buttons
            widget.configure(fg_color=theme.ACCENT if held else theme.PANEL_ALT,
                             text_color=theme.ACCENT_TEXT if held else theme.TEXT)
        if not self._manual_allowed():
            if (buttons or stick != (0.0, 0.0)) and not self._warned_no_input:
                self._warned_no_input = True
                logger.info('input ignored: %s', 'the bot is in control' if self._busy() in
                            ('hunt', 'test') else 'controller not connected')
            return
        self._warned_no_input = False
        self.worker.call(apply_to_controller, self.controller, buttons, stick)
        if self.recorder:
            self.recorder.update(buttons)
            if stick != (0.0, 0.0) and not self._warned_stick_recording:
                self._warned_stick_recording = True
                logger.warning('stick movements are not recorded; use the D-pad (arrow keys)')

    def _release_all(self) -> None:
        for after_id in self._pending_release.values():
            self.root.after_cancel(after_id)
        self._pending_release.clear()
        if self.input.clear():
            self._apply_input()

    def _pad(self, button: str, down: bool) -> None:
        source = f'mouse:{button}'
        changed = self.input.press(source, button) if down else self.input.release(source)
        if changed:
            self._apply_input()

    def soft_reset(self) -> None:
        if not self._manual_allowed():
            logger.info('soft reset ignored: controller not connected or bot in control')
            return
        # Held like any other input, so it shows on the pad and gets recorded.
        for button in SOFT_RESET:
            self.input.press(f'softreset:{button}', button)
        self._apply_input()

        def release():
            for button in SOFT_RESET:
                self.input.release(f'softreset:{button}')
            self._apply_input()
        self.root.after(500, release)

    # -- sequences: choose, record, edit --------------------------------------

    def _refresh_sequences(self) -> None:
        self.sequence_box.configure(values=list(self.config.sequences))
        self.sequence_var.set(self.config.active_sequence)

    def _choose_sequence(self) -> None:
        self.config.active_sequence = self.sequence_var.get()
        self.config.save()
        logger.info('using sequence "%s"', self.config.active_sequence)

    def _save_sequence(self, name: str, steps) -> None:
        self.config.sequences[name] = steps
        self.config.active_sequence = name
        self.config.save()
        self._refresh_sequences()
        logger.info('saved sequence "%s" (%d steps); it is now the active sequence', name, len(steps))

    def toggle_record(self) -> None:
        if self.recorder:
            steps = self.recorder.stop()
            self.recorder = None
            self.record_button.configure(text='Record')
            self.record_button.set_kind('normal')
            if not steps:
                logger.info('recording stopped: nothing was pressed')
                return
            logger.info('recording stopped: %d presses. Add notes, then Save as', len(steps))
            SequenceEditor(self.root, f'Recorded {time.strftime("%H-%M")}', steps,
                           on_save=self._save_sequence)
            return
        if not self._manual_allowed():
            messagebox.showinfo('Record', 'Connect the controller first (and stop any test or hunt).')
            return
        self._release_all()
        self.recorder = Recorder()
        self._warned_stick_recording = False
        self.record_button.configure(text='Stop recording')
        self.record_button.set_kind('danger')
        logger.info('recording: play the sequence with the keyboard (or the buttons on the Play '
                    'page), from the soft reset to the summary screen, then press Stop recording')

    def edit_sequence(self) -> None:
        SequenceEditor(self.root, self.config.active_sequence, self.config.sequence,
                       on_save=self._save_sequence)

    # -- keyboard -------------------------------------------------------------

    def _key_target(self, event) -> str | None:
        widget = event.widget
        if not isinstance(widget, tk.Misc) or widget.winfo_toplevel() is not self.root:
            return None  # e.g. the sequence editor window
        if isinstance(widget, (tk.Entry, tk.Spinbox, tk.Text)) and \
                str(widget.cget('state')) != 'disabled':
            return None  # typing into a field, not playing
        return self.input.key_map.get(event.keysym.lower())

    def _on_key_press(self, event):
        target = self._key_target(event)
        if target is None:
            return None
        key = event.keysym.lower()
        pending = self._pending_release.pop(key, None)
        if pending:
            self.root.after_cancel(pending)
        if self.input.press(f'key:{key}', target):
            self._apply_input()
        return 'break'

    def _on_key_release(self, event):
        if self._key_target(event) is None:
            return None
        key = event.keysym.lower()

        def release():
            self._pending_release.pop(key, None)
            if self.input.release(f'key:{key}'):
                self._apply_input()

        self._pending_release[key] = self.root.after(RELEASE_DEBOUNCE_MS, release)
        return 'break'

    def _check_focus(self) -> None:
        try:
            focused = self.root.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None:  # another application has focus
            self._release_all()

    # -- audio ----------------------------------------------------------------

    def _init_audio(self) -> None:
        try:
            self._audio_devices = list_inputs()
        except RuntimeError as error:
            self.audio_box.configure(values=[self.NO_AUDIO])
            self.audio_var.set(self.NO_AUDIO)
            logger.warning('%s', error)
            return
        self.audio_box.configure(values=[self.NO_AUDIO] + [d.name for d in self._audio_devices])
        saved = self.config.audio_device
        if saved == self.NO_AUDIO:
            self.audio_var.set(self.NO_AUDIO)
            return
        device = next((d for d in self._audio_devices if d.name == saved), None)
        if device is None:
            device = guess_capture_device(self._audio_devices)
            if device:
                logger.info('audio: guessed "%s" is the capture card; pick another on the '
                            'Setup page if not', device.name)
        self.audio_var.set(device.name if device else self.NO_AUDIO)
        if device:
            self._start_audio(device)

    def _start_audio(self, device) -> None:
        try:
            self.audio.start(device)
        except Exception as error:
            logger.warning('audio: could not play "%s" (%s)', device.name, error)

    def _choose_audio(self) -> None:
        name = self.audio_var.get()
        self.config.audio_device = name
        self.config.save()
        device = next((d for d in self._audio_devices if d.name == name), None)
        if device is None:
            self.audio.stop()
            logger.info('audio off')
        else:
            self._start_audio(device)

    def toggle_mute(self) -> None:
        self.mute_var.set(not self.audio.muted)
        self._mute_switched()

    def _mute_switched(self) -> None:
        self.audio.muted = bool(self.mute_var.get())
        self.config.audio_muted = self.audio.muted
        self.config.save()

    def _set_volume(self) -> None:
        self.audio.volume = self.volume_var.get() / 100
        self.config.audio_volume = round(self.audio.volume, 2)

    # -- camera / preview -----------------------------------------------------

    def _video_text(self, text: str) -> None:
        for video in (self.hunt_video, self.play_video):
            video.canvas.itemconfigure(video.text_item, text=text)

    def open_camera(self) -> None:
        try:
            camera = int(self.camera_var.get())
        except (tk.TclError, ValueError):
            return
        old, self.frames = self.frames, None
        self._video_text(f'opening camera {camera}')

        def work():
            if old:
                old.stop()
            grabber = FrameGrabber(camera, *self.config.frame_size)
            try:
                grabber.start()
            except Exception as error:
                grabber.stop()
                self.post(lambda: self._camera_failed(camera, error))
                return
            self.post(lambda: self._camera_opened(camera, grabber))

        threading.Thread(target=work, daemon=True).start()

    def _camera_opened(self, camera: int, grabber: FrameGrabber) -> None:
        self.frames = grabber
        self.config.camera = camera
        self.config.save()
        self._video_text('')
        logger.info('camera %d open', camera)

    def _camera_failed(self, camera: int, error: Exception) -> None:
        self._video_text(f'camera {camera} failed')
        logger.warning('camera %d: %s', camera, error)

    def _visible_video(self) -> Video | None:
        return {'hunt': self.hunt_video, 'play': self.play_video}.get(self.active_page)

    def _update_preview(self) -> None:
        video = self._visible_video()
        if not self.frames or video is None:
            return
        frame_time, frame = self.frames.peek()
        if frame is None or frame_time == self._frame_time:
            return
        self._frame, self._frame_time = frame, frame_time
        height, width = frame.shape[:2]
        self._scale = self.preview_width / width
        shown_height = round(height * self._scale)
        small = cv2.resize(frame, (self.preview_width, shown_height), interpolation=cv2.INTER_AREA)
        ok, ppm = cv2.imencode('.ppm', small)
        if not ok:
            return
        self._photo = tk.PhotoImage(data=ppm.tobytes(), format='PPM')
        video.canvas.itemconfigure(video.image_item, image=self._photo)
        if int(video.canvas.cget('height')) != shown_height:
            video.canvas.configure(height=shown_height)
        if video is self.hunt_video:
            self._draw_boxes()

    def _draw_boxes(self) -> None:
        self.canvas.delete('box')
        for box, color, text in ((self.config.sprite_box, theme.ACCENT, 'sprite'),
                                 (self.config.screen_box, '#7fe0d8', 'screen')):
            if box:
                x, y, w, h = (v * self._scale for v in box)
                self.canvas.create_rectangle(x, y, x + w, y + h, outline=color, width=2, tags='box')
                self.canvas.create_text(x + 4, y + 2, text=text, fill=color, anchor='nw', tags='box',
                                        font=F('small'))

    # -- marking regions --------------------------------------------------------

    def mark_box(self, which: str) -> None:
        if self._frame is None:
            messagebox.showinfo('Mark box', 'Open the camera first.')
            return
        self.select_page('hunt')
        self._box_mode = which
        what = ('around the Pokemon sprite only' if which == 'sprite' else
                'around something that never changes on the summary screen, e.g. the title bar')
        logger.info('drag a box on the video %s (with the summary screen showing)', what)
        self.canvas.configure(cursor='crosshair')

    def _drag_begin(self, event) -> None:
        if self._box_mode:
            self._drag_start = (event.x, event.y)

    def _drag_move(self, event) -> None:
        if self._box_mode and self._drag_start:
            self.canvas.delete('drag')
            x0, y0 = self._drag_start
            self.canvas.create_rectangle(x0, y0, event.x, event.y, outline='white', dash=(4, 2),
                                         tags='drag')

    def _drag_end(self, event) -> None:
        if not (self._box_mode and self._drag_start):
            return
        self.canvas.delete('drag')
        x0, y0 = self._drag_start
        which, self._box_mode, self._drag_start = self._box_mode, None, None
        self.canvas.configure(cursor='')
        x, y = min(x0, event.x) / self._scale, min(y0, event.y) / self._scale
        w, h = abs(event.x - x0) / self._scale, abs(event.y - y0) / self._scale
        if w < 4 or h < 4:
            logger.info('box too small; click "Mark %s box" and try again', which)
            return
        frame = self._frame
        box = tuple(int(round(v)) for v in (x, y, w, h))
        setattr(self.config, f'{which}_box', box)
        self.config.frame_size = (frame.shape[1], frame.shape[0])
        self.config.save()
        OUTPUT_DIR.mkdir(exist_ok=True)
        cv2.imwrite(str(OUTPUT_DIR / SETUP_FRAME), frame)
        self._draw_boxes()
        logger.info('%s box saved: %s', which, box)

    # -- bot jobs -------------------------------------------------------------

    def _start_job(self, name: str, make_coro, on_done=None) -> bool:
        if self.recorder:
            messagebox.showinfo('Recording', 'Stop recording first.')
            return False
        if self.job:
            messagebox.showinfo('Busy', f'Already running: {self.job[0]}. Press Stop first.')
            return False
        self._release_all()
        future = self.worker.submit(make_coro())
        self.job = (name, future)
        future.add_done_callback(lambda f: self.post(lambda: self._job_finished(name, f, on_done)))
        return True

    def _job_finished(self, name: str, future, on_done) -> None:
        self.job = None
        if self.controller is not None:
            self.worker.call(self.controller.release)
        if future.cancelled():
            logger.info('%s stopped', name)
            return
        error = future.exception()
        if isinstance(error, HuntStopped):
            logger.warning('hunt stopped: %s', error)
            messagebox.showwarning('Hunt stopped', str(error))
        elif error:
            logger.error('%s failed: %s', name, error)
        elif on_done:
            on_done(future.result())

    def _bot_ready(self) -> bool:
        if not (self.controller and self.controller.connected):
            messagebox.showinfo('Not ready', 'Connect the controller first (Setup page).')
            return False
        if not self.frames:
            messagebox.showinfo('Not ready', 'Open the camera first (Setup page).')
            return False
        # Pick up edits to shinybot.json (timings) without restarting.
        reloaded = Config.load()
        reloaded.camera = self.config.camera
        self.config = reloaded
        return True

    def test_sequence(self) -> None:
        if self._bot_ready():
            self._start_job('test', lambda: run_test_sequence(self.controller, self.frames, self.config))

    def start_hunt(self) -> None:
        if not self._bot_ready():
            return
        try:
            self.hunter = Hunter(self.controller, self.frames, self.config, OUTPUT_DIR)
        except HuntStopped as error:
            messagebox.showinfo('Not ready', str(error))
            return
        self._start_job('hunt', self.hunter.run, on_done=self._shiny_found)

    def stop_job(self) -> None:
        if self.job:
            self.job[1].cancel()

    def _shiny_found(self, result) -> None:
        _frame, check = result
        self.select_page('hunt')
        self.root.deiconify()
        self.root.lift()
        self.root.attributes('-topmost', True)
        self.root.after(500, lambda: self.root.attributes('-topmost', False))
        self.worker.submit(alert())
        messagebox.showinfo(
            'SHINY!', f'Shiny found after {self.hunter.stats.resets} resets!\n\n{check}\n\n'
                      'The bot has stopped. You can play from here (keyboard or buttons) '
                      'or take over with your own controller, and save.')

    # -- shutdown -------------------------------------------------------------

    def close(self) -> None:
        self.stop_job()

        async def shutdown():
            if self.controller:
                await self.controller.close()

        try:
            self.worker.submit(shutdown()).result(timeout=5)
        except Exception:
            pass
        if self.frames:
            self.frames.stop()
        self.audio.stop()
        self.worker.stop()
        self.root.after_cancel(self._tick_id)
        self.root.destroy()


def main(camera: int | None = None) -> None:
    theme.set_app_id()
    setup_logging(console=sys.stderr is not None)
    config = Config.load()
    if camera is not None:
        config.camera = camera
    root = ctk.CTk(fg_color=theme.BG)
    root.title('Shiny Bot - FireRed / LeafGreen')
    App(root, config)
    root.mainloop()


if __name__ == '__main__':
    main()
