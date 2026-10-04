"""Actions shared by the command line and the window."""

from __future__ import annotations

import asyncio
import logging
import sys
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
