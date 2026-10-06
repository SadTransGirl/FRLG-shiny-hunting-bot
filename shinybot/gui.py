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
from .app import (BEEPS, OUTPUT_DIR, LoopWatchdog, alert, alert_sound_path, install_crash_logging,
                  open_controller, run_test_sequence, setup_logging)
from .audio import AudioPassthrough, guess_capture_device, list_inputs
from .capture import FrameGrabber
from .config import Config
from .editor import SequenceEditor
from .glass import GlassFrame, GlassLabel, GlassPage
from .hunt import SETUP_FRAME, Hunter, HuntStopped
from .keymap import InputState, apply_to_controller
from .recorder import Recorder

logger = logging.getLogger('shinybot.gui')

SIDE_WIDTH = 320  # px (before display scaling) of the Hunt page's right column
DEFAULT_SIZE = (1200, 780)  # px before display scaling; capped to the screen
MIN_SIZE = (900, 600)
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
        self.thread = threading.Thread(target=self.loop.run_forever, name='bluetooth+hunt', daemon=True)
        self.thread.start()
        self.watchdog = LoopWatchdog(self.loop)

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn, *args) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        self.watchdog.stop()
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


CARD_ALPHA = 0.72   # how much of the wallpaper the cards hide (1 = solid)
INNER_ALPHA = 0.5   # boxes inside cards


class Row(GlassFrame):
    """An invisible layout container (shows whatever is behind it)."""


class Card(GlassFrame):
    """See-through rounded panel with an optional heading; put content in .body."""

    def __init__(self, parent, title: str | None = None, hint: str | None = None) -> None:
        super().__init__(parent, tint=theme.PANEL, alpha=CARD_ALPHA, radius=theme.RADIUS,
                         border=theme.BORDER)
        if title:
            label(self, title, font='heading').pack(fill='x', padx=16, pady=(10, 0))
        if hint:
            label(self, hint, muted=True, font='small', wrap=True).pack(fill='x', padx=16)
        self.body = Row(self)
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


def label(parent, text: str = '', muted: bool = False, font: str = 'body', **kw) -> GlassLabel:
    color = kw.pop('color', theme.TEXT_MUTED if muted else theme.TEXT)
    return GlassLabel(parent, text, font=font, color=color, **kw)


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


class Video(GlassFrame):
    """The capture card picture, scaled to fit the space it is given.

    It is a canvas so the region boxes can be drawn over the picture.
    """

    def __init__(self, parent, on_resize=None) -> None:
        super().__init__(parent, tint=theme.SIDEBAR, alpha=0.85, radius=10)
        self.configure(width=self.S(320), height=self.S(180))  # minimum; grows with the window
        self.pack_propagate(False)
        self.aspect = 16 / 9
        self.view = (self.S(320), self.S(180))
        self.on_resize = on_resize
        self.canvas = tk.Canvas(self, bg=theme.SIDEBAR, highlightthickness=0, bd=0)
        self.image_item = self.canvas.create_image(0, 0, anchor='nw')
        self.text_item = self.canvas.create_text(0, 0, fill=theme.TEXT_MUTED, text='no camera',
                                                 font=F('body'))
        self.bind('<Configure>', lambda _e: self.fit(), add='+')

    def fit(self, aspect: float | None = None) -> None:
        """Size the picture to the largest that fits, keeping the game's aspect ratio."""
        if aspect:
            self.aspect = aspect
        pad = self.S(6)
        avail_w = max(16, self.winfo_width() - 2 * pad)
        avail_h = max(9, self.winfo_height() - 2 * pad)
        width = min(avail_w, int(avail_h * self.aspect))
        height = int(width / self.aspect)
        if (width, height) == self.view and self.canvas.winfo_ismapped():
            return
        self.view = (width, height)
        self.canvas.place(relx=0.5, rely=0.5, anchor='center', width=width, height=height)
        self.canvas.coords(self.text_item, width // 2, height // 2)
        if self.on_resize:
            self.on_resize()


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
        self._build()
        logging.getLogger().addHandler(_UiLogHandler(self))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = (
            lambda *exc: logger.error('error in the window', exc_info=exc))
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
        content.pack(side='left', fill='both', expand=True)
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        wallpaper = theme.load_background(self.config.background)
        self.pages: dict[str, GlassPage] = {}
        self.nav_buttons = {}
        for key, text in PAGES:
            page = GlassPage(content, wallpaper)
            page.configure(width=1, height=1)  # size comes from the window, not the content
            page.grid(row=0, column=0, sticky='nsew')
            inner = Row(page)  # page margins
            inner.pack(fill='both', expand=True, padx=16, pady=14)
            self.pages[key] = page
            button = ctk.CTkButton(nav, text=text, compound='top', width=88, height=80, corner_radius=12,
                                   image=theme.icon(key, 30), fg_color='transparent',
                                   hover_color=theme.PANEL, text_color=theme.TEXT_MUTED, font=F('nav'),
                                   command=lambda k=key: self.select_page(k))
            button.pack(side='top', pady=(12 if key == 'hunt' else 4, 0))
            self.nav_buttons[key] = button
            getattr(self, f'_build_{key}')(inner)
        self.select_page('hunt')

    def _page_header(self, page, title: str, subtitle: str) -> Card:
        """Slim banner: title and subtitle on one line over an accent rule."""
        header = Card(page)
        header.body.pack_configure(pady=header.S((4, 8)))
        line = Row(header.body)
        line.pack(fill='x')
        label(line, title, font='heading').pack(side='left')
        label(line, subtitle, font='small', color=theme.ACCENT).pack(side='left', padx=(14, 0), pady=(8, 0))
        ctk.CTkFrame(header.body, fg_color=theme.ACCENT, height=3, corner_radius=2).pack(
            fill='x', pady=(4, 0))
        return header

    @staticmethod
    def _button_row(parent, buttons) -> Row:
        """Buttons sharing a row equally: [(text, command, kind), ...]."""
        row = Row(parent)
        for column, (text, command, kind) in enumerate(buttons):
            row.grid_columnconfigure(column, weight=1, uniform='buttons')
            Btn(row, text, command, kind=kind, width=10).grid(
                row=0, column=column, sticky='ew', padx=(0 if column == 0 else 6, 0))
        return row

    def _build_hunt(self, page) -> None:
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, minsize=page.S(SIDE_WIDTH))
        page.grid_rowconfigure(1, weight=3)
        page.grid_rowconfigure(2, weight=1, minsize=page.S(90))
        self._page_header(page, 'Hunt', 'soft reset, summary screen, shiny check, repeat').grid(
            row=0, column=0, columnspan=2, sticky='ew', pady=(0, 12))

        video_card = Card(page, 'Capture', 'Drag on the video after Sprite box / Screen box.')
        video_card.grid(row=1, column=0, sticky='nsew', padx=(0, 12))
        self.hunt_video = Video(video_card.body, on_resize=self._video_resized)
        self.hunt_video.pack(fill='both', expand=True)
        self.canvas = self.hunt_video.canvas
        self.canvas.bind('<ButtonPress-1>', self._drag_begin)
        self.canvas.bind('<B1-Motion>', self._drag_move)
        self.canvas.bind('<ButtonRelease-1>', self._drag_end)

        log_card = Card(page)
        log_card.grid(row=2, column=0, sticky='nsew', padx=(0, 12), pady=(12, 0))
        log_card.body.pack_configure(pady=log_card.S((12, 12)))
        self.log = ctk.CTkTextbox(log_card.body, font=F('mono'), fg_color=theme.SIDEBAR,
                                  text_color=theme.TEXT, corner_radius=10, wrap='word', height=40)
        self.log.pack(fill='both', expand=True)
        self.log.configure(state='disabled')

        side = Row(page)
        side.grid(row=1, column=1, rowspan=2, sticky='nsew')
        hunt = Card(side)
        hunt.pack(fill='x')
        hunt.body.pack_configure(pady=hunt.S((14, 14)))
        stats = GlassFrame(hunt.body, tint=theme.PANEL_ALT, alpha=INNER_ALPHA, radius=10)
        stats.pack(fill='x', pady=(0, 10))
        label(stats, 'Resets', muted=True, font='small').pack(anchor='w', padx=14, pady=(8, 0))
        self.resets_label = label(stats, '0', font='big', color=theme.ACCENT)
        self.resets_label.pack(anchor='w', padx=14)
        self.hunt_status = label(stats, 'not hunting', muted=True, font='small', wrap=True)
        self.hunt_status.pack(fill='x', padx=14, pady=(0, 8))
        self._button_row(hunt.body, [('Start hunt', self.start_hunt, 'primary'),
                                     ('Stop', self.stop_job, 'danger')]).pack(fill='x')

        sequence = Card(side, 'Sequence')
        sequence.pack(fill='x', pady=(12, 0))
        self.sequence_var = tk.StringVar(value=self.config.active_sequence)
        self.sequence_box = option(sequence.body, self.sequence_var, list(self.config.sequences),
                                   command=lambda _v: self._choose_sequence(), width=10)
        self.sequence_box.pack(fill='x', pady=(0, 8))
        row = self._button_row(sequence.body, [('Test', self.test_sequence, 'normal'),
                                               ('Record', self.toggle_record, 'normal'),
                                               ('Edit', self.edit_sequence, 'normal')])
        row.pack(fill='x')
        self.record_button = row.grid_slaves(row=0, column=1)[0]
        label(sequence.body, 'Regions (on the summary screen)', muted=True, font='small',
              wrap=True).pack(fill='x', pady=(10, 4))
        self._button_row(sequence.body, [('Sprite box', lambda: self.mark_box('sprite'), 'normal'),
                                         ('Screen box', lambda: self.mark_box('screen'), 'normal')]
                         ).pack(fill='x')

    def _build_play(self, page) -> None:
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self._page_header(page, 'Play', 'control the Switch from this window').grid(
            row=0, column=0, columnspan=2, sticky='ew', pady=(0, 12))
        video_card = Card(page, 'Capture')
        video_card.grid(row=1, column=0, sticky='nsew', padx=(0, 12))
        self.play_video = Video(video_card.body, on_resize=self._video_resized)
        self.play_video.pack(fill='both', expand=True)

        pad_card = Card(page, 'Controller', 'Click and hold, or use the keyboard.')
        pad_card.grid(row=1, column=1, rowspan=2, sticky='nsew')
        pad = Row(pad_card.body)
        pad.pack()
        self.pad_buttons: dict[str, ctk.CTkButton] = {}
        for text, button, row_index, column, symbol in PAD_LAYOUT:
            widget = ctk.CTkButton(pad, text=text, width=50 if len(text) < 3 else 76, height=38,
                                   corner_radius=10, font=F('symbol') if symbol else F('body'),
                                   fg_color=theme.PANEL_ALT, hover_color=theme.PANEL_HOVER,
                                   text_color=theme.TEXT)
            widget.grid(row=row_index, column=column, padx=2, pady=2)
            widget.bind('<ButtonPress-1>', lambda _e, b=button: self._pad(b, True), add='+')
            widget.bind('<ButtonRelease-1>', lambda _e, b=button: self._pad(b, False), add='+')
            self.pad_buttons[button] = widget
        self._button_row(pad_card.body, [('Soft reset', self.soft_reset, 'normal')]).pack(
            fill='x', pady=(10, 0))

        keys_card = Card(page, 'Keyboard', 'Works while this window is focused.')
        keys_card.grid(row=2, column=0, sticky='nsew', padx=(0, 12), pady=(12, 0))
        names = {'return': 'Enter', 'backspace': 'Bksp'}
        # Non-breaking spaces keep each "key: button" pair on one line when the text wraps.
        text = '    '.join(
            f"{names.get(key, key.capitalize())}: {target.replace('_', ' ').replace('LS', 'Stick')}"
            .replace(' ', '\u00a0') for key, target in self.config.keys.items())
        label(keys_card.body, text, font='small', wrap=True).pack(fill='x')

    def _build_setup(self, page) -> None:
        page.grid_columnconfigure((0, 1), weight=1, uniform='setup')
        self._page_header(page, 'Setup', 'controller, capture card and sound').grid(
            row=0, column=0, columnspan=2, sticky='ew', pady=(0, 12))

        controller = Card(page, 'Controller', 'The USB Bluetooth adapter pretends to be a Pro '
                                              'Controller. Pair once, then Connect each session.')
        controller.grid(row=1, column=0, sticky='nsew', padx=(0, 6), pady=(0, 12))
        self.status = label(controller.body, 'not connected', muted=True, wrap=True)
        self.status.pack(fill='x', pady=(0, 8))
        self._button_row(controller.body, [('Connect', self.connect, 'primary'),
                                           ('Pair', self.pair, 'normal')]).pack(fill='x')

        camera = Card(page, 'Capture card', 'If OBS has the card, close it or use its Virtual Camera.')
        camera.grid(row=1, column=1, sticky='nsew', padx=(6, 0), pady=(0, 12))
        row = Row(camera.body)
        row.pack(fill='x')
        label(row, 'Camera').pack(side='left', padx=(0, 8))
        self.camera_var = tk.StringVar(value=str(self.config.camera))
        option(row, self.camera_var, [str(i) for i in range(10)], width=80).pack(side='left')
        Btn(row, 'Open', self.open_camera, width=90).pack(side='left', padx=8)

        audio = Card(page, 'Sound', 'Plays the capture card through your speakers. '
                                    'Muting only affects this PC.')
        audio.grid(row=2, column=0, sticky='nsew', padx=(0, 6))
        self.audio_var = tk.StringVar(value=self.NO_AUDIO)
        self.audio_box = option(audio.body, self.audio_var, [self.NO_AUDIO],
                                command=lambda _v: self._choose_audio(), width=10)
        self.audio_box.pack(fill='x', pady=(0, 10))
        row = Row(audio.body)
        row.pack(fill='x')
        self.mute_var = tk.BooleanVar(value=self.audio.muted)
        self.mute_switch = ctk.CTkSwitch(row, text='', width=46, variable=self.mute_var, onvalue=True,
                                         offvalue=False, command=self._mute_switched,
                                         progress_color=theme.ACCENT, fg_color=theme.BORDER,
                                         button_color='#dfe6f2', button_hover_color='#ffffff',
                                         switch_width=40, switch_height=20)
        self.mute_switch.pack(side='left')
        label(row, 'Mute').pack(side='left', padx=(0, 16))
        label(row, 'Volume', muted=True).pack(side='left', padx=(0, 8))
        self.volume_var = tk.DoubleVar(value=self.config.audio_volume * 100)
        volume = ctk.CTkSlider(row, from_=0, to=100, variable=self.volume_var, width=80,
                               command=lambda _v: self._set_volume(), progress_color=theme.ACCENT,
                               button_color=theme.ACCENT, button_hover_color=theme.ACCENT_HOVER,
                               fg_color=theme.PANEL_ALT)
        volume.pack(side='left', fill='x', expand=True)
        volume.bind('<ButtonRelease-1>', lambda _e: self.config.save(), add='+')

        look = Card(page, 'Background and alert')
        look.grid(row=2, column=1, sticky='nsew', padx=(6, 0))
        label(look.body, 'Background picture (darkened behind the panels)', muted=True, font='small',
              wrap=True).pack(fill='x', pady=(0, 4))
        self._button_row(look.body, [('Choose picture', self.choose_background, 'normal'),
                                     ('Default', lambda: self.set_background(None), 'normal')]
                         ).pack(fill='x')
        self.alert_label = label(look.body, '', muted=True, font='small', wrap=True)
        self.alert_label.pack(fill='x', pady=(12, 4))
        self._button_row(look.body, [('Choose sound', self.choose_alert_sound, 'normal'),
                                     ('Test', self.test_alert, 'normal')]).pack(fill='x')
        self._button_row(look.body, [('Default', lambda: self.set_alert_sound(None), 'normal'),
                                     ('Beeps', lambda: self.set_alert_sound(BEEPS), 'normal')]
                         ).pack(fill='x', pady=(6, 0))
        self._show_alert_sound()
        label(look.body, 'Font: "Pokemon Pixel Font" by SpyroSteak (CC BY-SA).', muted=True,
              font='small', wrap=True).pack(fill='x', pady=(12, 0))

    def _show_alert_sound(self) -> None:
        path = alert_sound_path(self.config.alert_sound)
        if path is None:
            text = 'Shiny alert: beeps'
        elif self.config.alert_sound:
            text = f'Shiny alert: {path.stem}'
        else:
            text = 'Shiny alert: default sound'
        self.alert_label.set(text)

    def choose_alert_sound(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title='Shiny alert sound',
            filetypes=[('Sounds', '*.mp3 *.wav *.wma *.m4a'), ('All files', '*.*')])
        if path:
            self.set_alert_sound(path)

    def set_alert_sound(self, sound: str | None) -> None:
        self.config.alert_sound = sound
        self.config.save()
        self._show_alert_sound()

    def test_alert(self) -> None:
        self.worker.submit(alert(self.config.alert_sound))

    def choose_background(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title='Background picture',
            filetypes=[('Pictures', '*.png *.jpg *.jpeg *.webp *.bmp'), ('All files', '*.*')])
        if path:
            self.set_background(path)

    def set_background(self, path: str | None) -> None:
        self.config.background = path
        self.config.save()
        wallpaper = theme.load_background(path)
        for page in self.pages.values():
            page.set_wallpaper(wallpaper)

    def select_page(self, key: str) -> None:
        tk.Misc.tkraise(self.pages[key])  # Canvas.tkraise raises drawing items, not the widget
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
        if self.status.text != text or self.status.color != color:
            self.status.set(text, color)
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
            if self.resets_label.text != resets:
                self.resets_label.set(resets)
            state = 'hunting' if busy == 'hunt' else 'stopped'
            status = f'{state}  -  last: {last.verdict}' if last else state
            if self.hunt_status.text != status:
                self.hunt_status.set(status)

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

    def _video_resized(self) -> None:
        self._frame_time = 0.0  # redraw at the new size

    def _update_preview(self) -> None:
        video = self._visible_video()
        if not self.frames or video is None:
            return
        frame_time, frame = self.frames.peek()
        if frame is None or frame_time == self._frame_time:
            return
        self._frame, self._frame_time = frame, frame_time
        height, width = frame.shape[:2]
        if abs(video.aspect - width / height) > 0.01:
            video.fit(width / height)
        view_w, view_h = video.view
        small = cv2.resize(frame, (view_w, view_h), interpolation=cv2.INTER_AREA)
        ok, ppm = cv2.imencode('.ppm', small)
        if not ok:
            return
        self._photo = tk.PhotoImage(data=ppm.tobytes(), format='PPM')
        video.canvas.itemconfigure(video.image_item, image=self._photo)
        if video is self.hunt_video:
            self._scale = view_w / width
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
        self.worker.submit(alert(self.config.alert_sound))
        messagebox.showinfo(
            'SHINY!', f'Shiny found after {self.hunter.stats.resets} resets!\n\n{check}\n\n'
                      'The bot has stopped. You can play from here (keyboard or buttons) '
                      'or take over with your own controller, and save.')

    # -- shutdown -------------------------------------------------------------

    def close(self) -> None:
        logger.info('window closed')
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
    install_crash_logging()
    logger.info('ShinyBot started')
    config = Config.load()
    if camera is not None:
        config.camera = camera
    root = ctk.CTk(fg_color=theme.BG)
    root.title('Shiny Bot - FireRed / LeafGreen')
    fit_to_screen(root)
    App(root, config)
    root.mainloop()


def fit_to_screen(root) -> None:
    """Default window size, never larger than ~90% of the screen."""
    scaling = ctk.ScalingTracker.get_window_scaling(root)
    screen_w = root.winfo_screenwidth() / scaling  # CustomTkinter sizes are before scaling
    screen_h = root.winfo_screenheight() / scaling
    width = int(min(DEFAULT_SIZE[0], screen_w * 0.9))
    height = int(min(DEFAULT_SIZE[1], screen_h * 0.85))
    root.geometry(f'{width}x{height}+{int((screen_w - width) / 2)}+{int((screen_h - height) / 3)}')
    root.minsize(min(MIN_SIZE[0], width), min(MIN_SIZE[1], height))


if __name__ == '__main__':
    main()
