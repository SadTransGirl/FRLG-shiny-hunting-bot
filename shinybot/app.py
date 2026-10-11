"""Actions shared by the command line and the window."""

from __future__ import annotations

import asyncio
import atexit
import faulthandler
import json
import logging
import os
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
    errors = logging.StreamHandler(_crash_file)  # crash.log: errors too, not only native crashes
    errors.setLevel(logging.ERROR)
    errors.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.getLogger().addHandler(errors)


SESSION_FILE = OUTPUT_DIR / 'session.json'


def memory_mb() -> float | None:
    """This process's memory use (working set) in MB, or None if it can't be read."""
    try:
        import psutil
        return psutil.Process().memory_info().rss / 2**20
    except ImportError:
        pass
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
                    'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.kernel32.K32GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize / 2**20
        return None
    try:
        with open('/proc/self/statm') as f:
            return int(f.read().split()[1]) * os.sysconf('SC_PAGE_SIZE') / 2**20
    except (OSError, ValueError):
        return None


def keep_awake(on: bool) -> None:
    """While hunting, ask Windows not to sleep (the screen may still turn off). No-op elsewhere."""
    if sys.platform != 'win32':
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def _clock(stamp: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp))


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f'{minutes // 60}h{minutes % 60:02d}m' if minutes >= 60 else f'{minutes}m{int(seconds % 60):02d}s'


class SessionMonitor:
    """Leaves a trail, so a run that dies without a Python error still explains itself.

    session.json is rewritten every `interval` seconds (time, what the bot is doing, memory)
    and marked closed when the program exits normally. If the next start finds it open, the
    process was killed (the PC shut down, restarted for an update or ran out of memory, or
    Task Manager ended it) or crashed inside native code (then crash.log has a dump), and that
    gets reported. A heartbeat that comes far too late means the PC slept or the bot froze.
    """

    def __init__(self, path: Path = SESSION_FILE, interval: float = 30.0) -> None:
        self.path, self.interval = Path(path), interval
        self.state = 'starting'
        self.started = time.time()
        self.peak_mb = 0.0
        self._lock = threading.Lock()
        self._closed = False
        self.previous = self.check_previous()
        self.beat()
        atexit.register(self.end, 'the program exited')
        self._stop = threading.Event()
        threading.Thread(target=self._run, name='session', daemon=True).start()

    def check_previous(self) -> str | None:
        """The report on an earlier run that ended without closing its session (also logged)."""
        try:
            last = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if last.get('closed'):
            return None
        started, beat = last.get('started', 0), last.get('last_beat', 0)
        memory = f', memory {last["memory_mb"]:.0f} MB (peak {last.get("peak_mb", 0):.0f})' if last.get('memory_mb') else ''
        report = (f'The previous run (started {_clock(started)}) ended without closing normally. '
                  f'Last sign of life: {_clock(beat)}, {_span(beat - started)} in, while '
                  f'"{last.get("state", "?")}"{memory}. No Python error stopped it, so the program was '
                  'killed or crashed inside a driver: the PC shut down or restarted (Windows Update), '
                  'ran out of memory, or Task Manager ended it. Check crash.log for a native '
                  'crash dump, and Windows Event Viewer > Windows Logs > System/Application around that time.')
        logger.error(report)
        return report

    def set_state(self, state: str) -> None:
        self.state = state

    def beat(self) -> None:
        memory = memory_mb()
        if memory:
            self.peak_mb = max(self.peak_mb, memory)
        self._write({'pid': os.getpid(), 'started': self.started, 'last_beat': time.time(),
                     'state': self.state, 'memory_mb': memory, 'peak_mb': self.peak_mb, 'closed': False})

    def end(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_thread()
        self._write({'pid': os.getpid(), 'started': self.started, 'last_beat': time.time(),
                     'state': self.state, 'peak_mb': self.peak_mb, 'closed': True, 'reason': reason})
        logger.info('session closed: %s after %s', reason, _span(time.time() - self.started))

    def _stop_thread(self) -> None:
        stop = getattr(self, '_stop', None)
        if stop is not None:
            stop.set()

    def _write(self, record: dict) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix('.tmp')
                temp.write_text(json.dumps(record), encoding='utf-8')
                os.replace(temp, self.path)  # never a half-written file, even if killed mid-write
            except OSError as error:
                logger.warning('could not write %s: %s', self.path, error)

    def _run(self) -> None:
        last = time.time()
        report_every, since_report = 30 * 60, 0.0
        while not self._stop.wait(self.interval):
            now = time.time()
            if now - last > max(3 * self.interval, 120):
                logger.warning('no heartbeat from %s to %s (%s): the PC was probably asleep, or the '
                               'program was frozen', _clock(last), _clock(now), _span(now - last))
            since_report += now - last
            last = now
            self.beat()
            if since_report >= report_every:
                since_report = 0.0
                memory = memory_mb()
                logger.info('still running after %s: %s%s', _span(now - self.started), self.state,
                            f', memory {memory:.0f} MB' if memory else '')


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
