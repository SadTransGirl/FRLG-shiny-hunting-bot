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
import tkinter as tk
from tkinter import messagebox, ttk

import cv2

from .app import OUTPUT_DIR, alert, open_controller, run_test_sequence, setup_logging
from .capture import FrameGrabber
from .config import Config
from .hunt import SETUP_FRAME, Hunter, HuntStopped
from .keymap import InputState, apply_to_controller

logger = logging.getLogger('shinybot.gui')

PREVIEW_WIDTH = 640
SOFT_RESET = ('A', 'B', 'PLUS', 'MINUS')
# Ignore a key release if the same key is pressed again within this many ms
# (X11 sends release/press pairs for auto-repeat).
RELEASE_DEBOUNCE_MS = 40

# (label, button, row, column) of the on-screen controller
PAD_LAYOUT = [
    ('ZL', 'ZL', 0, 0), ('L', 'L', 0, 1), ('R', 'R', 0, 4), ('ZR', 'ZR', 0, 5),
    ('−', 'MINUS', 1, 1), ('Capture', 'CAPTURE', 1, 2), ('Home', 'HOME', 1, 3), ('+', 'PLUS', 1, 4),
    ('▲', 'UP', 2, 1), ('X', 'X', 2, 4),
    ('◀', 'LEFT', 3, 0), ('▶', 'RIGHT', 3, 2), ('Y', 'Y', 3, 3), ('A', 'A', 3, 5),
    ('▼', 'DOWN', 4, 1), ('B', 'B', 4, 4),
]


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


class App:
    def __init__(self, root: tk.Tk, config: Config) -> None:
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

        self._build()
        logging.getLogger().addHandler(_UiLogHandler(self))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind_all('<KeyPress>', self._on_key_press)
        root.bind_all('<KeyRelease>', self._on_key_release)
        root.bind('<FocusOut>', lambda _e: root.after(50, self._check_focus))
        root.after(30, self._tick)
        self.open_camera()

    # -- layout ---------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        top = ttk.Frame(root, padding=(8, 6))
        top.pack(fill='x')
        ttk.Label(top, text='Controller:').pack(side='left')
        self.status = tk.Label(top, text='not connected', fg='gray', width=34, anchor='w')
        self.status.pack(side='left', padx=(4, 8))
        ttk.Button(top, text='Connect', command=self.connect, takefocus=False).pack(side='left')
        ttk.Button(top, text='Pair…', command=self.pair, takefocus=False).pack(side='left', padx=4)
        ttk.Separator(top, orient='vertical').pack(side='left', fill='y', padx=10)
        ttk.Label(top, text='Camera:').pack(side='left')
        self.camera_var = tk.IntVar(value=self.config.camera)
        ttk.Spinbox(top, from_=0, to=9, width=3, textvariable=self.camera_var).pack(side='left', padx=4)
        ttk.Button(top, text='Open', command=self.open_camera, takefocus=False).pack(side='left')

        body = ttk.Frame(root, padding=(8, 0))
        body.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(body, width=PREVIEW_WIDTH, height=360, bg='black',
                                highlightthickness=3, highlightbackground='#444',
                                highlightcolor='#2a9d4b')
        self.canvas.grid(row=0, column=0, sticky='n')
        self._image_item = self.canvas.create_image(0, 0, anchor='nw')
        self._canvas_text = self.canvas.create_text(PREVIEW_WIDTH // 2, 180, fill='white',
                                                    text='No camera')
        self.canvas.bind('<ButtonPress-1>', self._drag_begin)
        self.canvas.bind('<B1-Motion>', self._drag_move)
        self.canvas.bind('<ButtonRelease-1>', self._drag_end)

        side = ttk.Frame(body, padding=(12, 0))
        side.grid(row=0, column=1, sticky='n')
        pad = ttk.Frame(side)
        pad.pack()
        self.pad_buttons: dict[str, tk.Button] = {}
        for label, button, row, column in PAD_LAYOUT:
            widget = tk.Button(pad, text=label, width=6, takefocus=False)
            widget.grid(row=row, column=column, padx=2, pady=2)
            widget.bind('<ButtonPress-1>', lambda _e, b=button: self._pad(b, True))
            widget.bind('<ButtonRelease-1>', lambda _e, b=button: self._pad(b, False))
            self.pad_buttons[button] = widget
        self._pad_default_bg = widget.cget('background')
        tk.Button(pad, text='Soft reset (A+B+Start+Select)', takefocus=False,
                  command=self.soft_reset).grid(row=5, column=0, columnspan=6, sticky='ew', pady=(6, 0))
        ttk.Label(side, text='Keyboard (click the video first):').pack(anchor='w', pady=(10, 2))
        keys = ttk.Frame(side)
        keys.pack(anchor='w')
        names = {'return': 'Enter', 'backspace': 'Bksp'}
        entries = list(self.config.keys.items())
        half = (len(entries) + 1) // 2
        for index, (key, target) in enumerate(entries):
            ttk.Label(keys, text=f'{names.get(key, key.capitalize()):>6} → {target.replace("_", " ")}',
                      font=('Consolas', 9)).grid(row=index % half, column=index // half,
                                                  sticky='w', padx=(0, 16))

        hunt = ttk.LabelFrame(root, text='Shiny hunt', padding=(8, 6))
        hunt.pack(fill='x', padx=8, pady=6)
        for text, command in [('Test sequence', self.test_sequence),
                              ('Mark sprite box', lambda: self.mark_box('sprite')),
                              ('Mark screen box', lambda: self.mark_box('screen')),
                              ('Start hunt', self.start_hunt), ('Stop', self.stop_job)]:
            ttk.Button(hunt, text=text, command=command, takefocus=False).pack(side='left', padx=(0, 4))
        self.hunt_status = ttk.Label(hunt, text='')
        self.hunt_status.pack(side='left', padx=10)

        log_frame = ttk.Frame(root, padding=(8, 0, 8, 8))
        log_frame.pack(fill='both', expand=True)
        self.log = tk.Text(log_frame, height=9, state='disabled', takefocus=False, wrap='word')
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')

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
        self.root.after(30, self._tick)

    # -- controller -----------------------------------------------------------

    def _busy(self) -> str | None:
        return self.job[0] if self.job else None

    def _update_status(self) -> None:
        busy = self._busy()
        if busy == 'pair':
            text, color = 'waiting: open Controllers > Change Grip/Order', '#b26b00'
        elif busy == 'connect':
            text, color = 'connecting…', '#b26b00'
        elif self.controller and self.controller.connected:
            text, color = 'connected', '#2a9d4b'
        else:
            text, color = 'not connected', 'gray'
        if busy in ('hunt', 'test'):
            text += ' (bot is in control)'
        self.status.configure(text=text, fg=color)
        if self.hunter:
            last = self.hunter.last_result
            self.hunt_status.configure(
                text=f'Resets: {self.hunter.stats.resets}' + (f'   last: {last.verdict}' if last else ''))

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
            widget.configure(relief='sunken' if name in buttons else 'raised',
                             background='#9fd5ad' if name in buttons else self._pad_default_bg)
        if not self._manual_allowed():
            if (buttons or stick != (0.0, 0.0)) and not self._warned_no_input:
                self._warned_no_input = True
                logger.info('input ignored: %s', 'the bot is in control' if self._busy() in
                            ('hunt', 'test') else 'controller not connected')
            return
        self._warned_no_input = False
        self.worker.call(apply_to_controller, self.controller, buttons, stick)

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
        self.worker.submit(self.controller.press(*SOFT_RESET, duration=0.5))

    # -- keyboard -------------------------------------------------------------

    def _key_target(self, event) -> str | None:
        if isinstance(event.widget, (tk.Entry, ttk.Entry, tk.Spinbox, ttk.Spinbox)):
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

    # -- camera / preview -----------------------------------------------------

    def open_camera(self) -> None:
        try:
            camera = self.camera_var.get()
        except tk.TclError:
            return
        old, self.frames = self.frames, None
        self.canvas.itemconfigure(self._canvas_text, text=f'Opening camera {camera}…')

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
        self.canvas.itemconfigure(self._canvas_text, text='')
        self.canvas.focus_set()
        logger.info('camera %d open', camera)

    def _camera_failed(self, camera: int, error: Exception) -> None:
        self.canvas.itemconfigure(self._canvas_text, text=f'Camera {camera} failed')
        logger.warning('camera %d: %s', camera, error)

    def _update_preview(self) -> None:
        if not self.frames:
            return
        frame_time, frame = self.frames.peek()
        if frame is None or frame_time == self._frame_time:
            return
        self._frame, self._frame_time = frame, frame_time
        height, width = frame.shape[:2]
        self._scale = PREVIEW_WIDTH / width
        shown_height = round(height * self._scale)
        small = cv2.resize(frame, (PREVIEW_WIDTH, shown_height), interpolation=cv2.INTER_AREA)
        ok, ppm = cv2.imencode('.ppm', small)
        if not ok:
            return
        self._photo = tk.PhotoImage(data=ppm.tobytes(), format='PPM')
        self.canvas.itemconfigure(self._image_item, image=self._photo)
        if int(self.canvas.cget('height')) != shown_height:
            self.canvas.configure(height=shown_height)
        self._draw_boxes()

    def _draw_boxes(self) -> None:
        self.canvas.delete('box')
        for box, color, label in ((self.config.sprite_box, '#ffd000', 'sprite'),
                                  (self.config.screen_box, '#00c8ff', 'screen')):
            if box:
                x, y, w, h = (v * self._scale for v in box)
                self.canvas.create_rectangle(x, y, x + w, y + h, outline=color, width=2, tags='box')
                self.canvas.create_text(x + 3, y + 2, text=label, fill=color, anchor='nw', tags='box')

    # -- marking regions --------------------------------------------------------

    def mark_box(self, which: str) -> None:
        if self._frame is None:
            messagebox.showinfo('Mark box', 'Open the camera first.')
            return
        self._box_mode = which
        what = ('around the Pokemon sprite only' if which == 'sprite' else
                'around something that never changes on the summary screen, e.g. the title bar')
        logger.info('drag a box on the video %s (with the summary screen showing)', what)
        self.canvas.configure(cursor='crosshair')

    def _drag_begin(self, event) -> None:
        self.canvas.focus_set()  # clicking the video gives keyboard control
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
            messagebox.showinfo('Not ready', 'Connect the controller first.')
            return False
        if not self.frames:
            messagebox.showinfo('Not ready', 'Open the camera first.')
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
        self.worker.stop()
        self.root.destroy()


def main(camera: int | None = None) -> None:
    if sys.platform == 'win32':
        try:  # crisp text on high-DPI screens
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    setup_logging(console=sys.stderr is not None)
    config = Config.load()
    if camera is not None:
        config.camera = camera
    root = tk.Tk()
    root.title('FRLG Shiny Bot')
    App(root, config)
    root.mainloop()


if __name__ == '__main__':
    main()
