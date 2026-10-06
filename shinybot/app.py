"""Actions shared by the command line and the window."""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

import cv2

from bt_controller import ProController

from .config import Config
from .sequence import run_steps, sequence_duration

OUTPUT_DIR = Path('shinybot_output')
KEYS_FILE = 'switch_pairing.json'

logger = logging.getLogger('shinybot')


def setup_logging(console: bool = True) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(OUTPUT_DIR / 'hunt.log', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    root.addHandler(file_handler)
    if console:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        handler.addFilter(lambda r: r.name.startswith(('shinybot', 'bt_controller')))
        root.addHandler(handler)


_crash_file = None  # kept open so faulthandler can write to it during a crash


def install_crash_logging() -> None:
    """Record crashes in hunt.log / crash.log (ShinyBot.pyw has no console to show them)."""
    global _crash_file
    OUTPUT_DIR.mkdir(exist_ok=True)
    _crash_file = open(OUTPUT_DIR / 'crash.log', 'a', encoding='utf-8')
    faulthandler.enable(_crash_file)  # hard crashes inside OpenCV, the USB driver, ...

    def thread_error(args) -> None:
        name = args.thread.name if args.thread else '?'
        logger.critical('unexpected error in thread %s', name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_error
    sys.excepthook = lambda *exc: logger.critical('unexpected error', exc_info=exc)


def _thread_stacks() -> str:
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    return '\n'.join(f'--- thread {names.get(ident, ident)}\n' + ''.join(traceback.format_stack(frame))
                     for ident, frame in sys._current_frames().items())


class LoopWatchdog:
    """Logs where the program is stuck if the asyncio loop (Bluetooth + hunt) stops running.

    While that loop is stuck no reports reach the Switch, so it keeps seeing the last buttons
    (a held soft reset shows as a black screen).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, stall_after: float = 10.0) -> None:
        self.loop = loop
        self.stall_after = stall_after
        self._beat = time.monotonic()
        self._stop = threading.Event()
        threading.Thread(target=self._run, name='watchdog', daemon=True).start()

    def _touch(self) -> None:
        self._beat = time.monotonic()

    def _run(self) -> None:
        stalled = False
        while not self._stop.wait(1.0):
            try:
                self.loop.call_soon_threadsafe(self._touch)
            except RuntimeError:  # loop closed
                return
            late = time.monotonic() - self._beat
            if late > self.stall_after and not stalled:
                stalled = True
                logger.error('the Bluetooth/hunt loop has been stuck for %.0f s. Where each '
                             'thread is:\n%s', late, _thread_stacks())
            elif stalled and late < 2:
                stalled = False
                logger.warning('the Bluetooth/hunt loop is running again')

    def stop(self) -> None:
        self._stop.set()


async def open_controller(config: Config) -> ProController:
    controller = ProController(config.transport, KEYS_FILE)
    await controller.open()
    return controller


async def connect_controller(config: Config) -> ProController:
    controller = await open_controller(config)
    try:
        await controller.reconnect()
    except Exception as error:
        await controller.close()
        raise RuntimeError(
            f'could not connect to the Switch ({error}). Make sure it is awake; if it was never '
            'paired, pair it first.'
        ) from error
    return controller


async def run_test_sequence(controller, frames, config: Config) -> Path:
    """Run the sequence once, saving a screenshot after every step."""
    folder = OUTPUT_DIR / 'test_sequence'
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob('*.png'):
        old.unlink()

    async def screenshot(index, step):
        name = f'{index + 1:02d}_{step.note or step.press}'.replace(' ', '_')
        name = ''.join(c for c in name if c.isalnum() or c in '_-+')
        cv2.imwrite(str(folder / f'{name}.png'), await asyncio.to_thread(frames.latest))
        logger.info('step %d: %s x%d (%s)', index + 1, step.press, step.repeat, step.note)

    logger.info('running the sequence once (~%.0fs)...', sequence_duration(config.sequence))
    await run_steps(controller, config.sequence, on_step=screenshot)
    logger.info('test run done; screenshots after each step are in %s', folder)
    return folder


DEFAULT_ALERT_SOUND = Path(__file__).parent / 'assets' / 'shiny_alert.mp3'
BEEPS = 'beeps'  # alert_sound value meaning "no sound file, just beep"


def alert_sound_path(setting: str | None) -> Path | None:
    """The sound file for the shiny alert: the chosen file, the bundled one, or None (beeps)."""
    if setting == BEEPS:
        return None
    path = Path(setting) if setting else DEFAULT_ALERT_SOUND
    return path if path.exists() else None


def play_sound_file(path: Path) -> None:
    """Play an MP3/WAV to the end (blocking) with Windows' built-in player (MCI)."""
    import ctypes
    import threading

    winmm = ctypes.windll.winmm
    alias = f'shinybot{threading.get_ident()}'

    def send(command: str) -> None:
        error = winmm.mciSendStringW(command, None, 0, 0)
        if error:
            text = ctypes.create_unicode_buffer(256)
            winmm.mciGetErrorStringW(error, text, 255)
            raise RuntimeError(text.value or f'MCI error {error}')

    send(f'open "{path}" type mpegvideo alias {alias}')
    try:
        send(f'play {alias} wait')
    finally:
        winmm.mciSendStringW(f'close {alias}', None, 0, 0)


async def alert(sound: str | None = None) -> None:
    """Shiny alert: play the alert sound (see alert_sound_path), else beep."""
    if sys.platform != 'win32':
        print('\a')
        return
    path = alert_sound_path(sound)
    if path is not None:
        try:
            await asyncio.to_thread(play_sound_file, path)
            return
        except Exception as error:
            logger.warning('could not play %s (%s); beeping instead', path, error)
    import winsound

    for _ in range(5):
        await asyncio.to_thread(winsound.Beep, 1200, 300)
        await asyncio.sleep(0.2)
