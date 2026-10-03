"""Shiny hunting bot: soft resets a starter in FireRed/LeafGreen until it's shiny.

    python -m shinybot cameras         list capture devices
    python -m shinybot preview         show the capture card picture
    python -m shinybot test-sequence   run one reset, screenshotting every step
    python -m shinybot setup           mark the sprite and screen regions
    python -m shinybot hunt            start hunting
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import cv2

from bt_controller import ProController

from .capture import FrameGrabber, list_cameras
from .config import CONFIG_FILE, Config
from .hunt import SETUP_FRAME, Hunter, HuntStopped
from .sequence import run_steps, sequence_duration

OUTPUT_DIR = Path('shinybot_output')
KEYS_FILE = 'switch_pairing.json'
MAX_WINDOW_WIDTH = 1280

logger = logging.getLogger('shinybot')


def setup_logging() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(OUTPUT_DIR / 'hunt.log', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    console.addFilter(lambda r: r.name.startswith(('shinybot', 'bt_controller')))
    root.addHandler(file_handler)
    root.addHandler(console)


async def connect_controller(config: Config) -> ProController:
    controller = ProController(config.transport, KEYS_FILE)
    await controller.open()
    try:
        await controller.reconnect()
    except Exception as error:
        await controller.close()
        raise RuntimeError(
            f'could not connect to the Switch ({error}). Make sure it is awake; if it was never '
            'paired, run "python -m bt_controller pair" first.'
        ) from error
    return controller


def _scaled(frame):
    scale = min(1.0, MAX_WINDOW_WIDTH / frame.shape[1])
    if scale < 1.0:
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return frame, scale


def cmd_cameras(_config: Config) -> None:
    cameras = list_cameras()
    if not cameras:
        print('No cameras found. Is the capture card plugged in (and not in use by OBS)?')
    for index, width, height in cameras:
        print(f'camera {index}: {width}x{height}')
    print('Use "python -m shinybot preview --camera N" to see which one is the Switch.')


def cmd_preview(config: Config) -> None:
    with FrameGrabber(config.camera, *config.frame_size) as frames:
        print('Showing camera', config.camera, '- press S to save a screenshot, Q to quit.')
        while True:
            frame = frames.latest()
            cv2.imshow('shinybot preview', _scaled(frame)[0])
            key = cv2.waitKey(30) & 0xFF
            if key in (ord('q'), 27):
                break
            if key == ord('s'):
                OUTPUT_DIR.mkdir(exist_ok=True)
                cv2.imwrite(str(OUTPUT_DIR / 'preview.png'), frame)
                print('saved', OUTPUT_DIR / 'preview.png')
    cv2.destroyAllWindows()


def _select_box(frame, title: str):
    shown, scale = _scaled(frame)
    print(f'{title}\n  Drag a box, then press ENTER (or C to cancel).')
    x, y, w, h = cv2.selectROI(title, shown, showCrosshair=False)
    cv2.destroyWindow(title)
    if w == 0 or h == 0:
        raise SystemExit('Cancelled.')
    return tuple(round(v / scale) for v in (x, y, w, h))


def cmd_setup(config: Config) -> None:
    with FrameGrabber(config.camera, *config.frame_size) as frames:
        print('Get the game to the starter\'s SUMMARY screen (run "python -m shinybot '
              'test-sequence" or use a controller), then press SPACE in the preview window.')
        while True:
            frame = frames.latest()
            cv2.imshow('shinybot setup', _scaled(frame)[0])
            key = cv2.waitKey(30) & 0xFF
            if key == ord(' '):
                break
            if key in (ord('q'), 27):
                raise SystemExit('Cancelled.')
        cv2.destroyAllWindows()
    config.frame_size = (frame.shape[1], frame.shape[0])
    config.sprite_box = _select_box(frame, 'Box 1: the Pokemon sprite only')
    config.screen_box = _select_box(
        frame, 'Box 2: something that is always the same on this screen (e.g. the title bar), '
               'not the sprite, name, gender or nature'
    )
    OUTPUT_DIR.mkdir(exist_ok=True)
    cv2.imwrite(str(OUTPUT_DIR / SETUP_FRAME), frame)
    config.save()
    print(f'Saved regions to {CONFIG_FILE}. Next: "python -m shinybot hunt".')


async def cmd_test_sequence(config: Config) -> None:
    folder = OUTPUT_DIR / 'test_sequence'
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob('*.png'):
        old.unlink()
    controller = await connect_controller(config)
    try:
        with FrameGrabber(config.camera, *config.frame_size) as frames:
            async def screenshot(index, step):
                name = f'{index + 1:02d}_{step.note or step.press}'.replace(' ', '_')
                name = ''.join(c for c in name if c.isalnum() or c in '_-+')
                cv2.imwrite(str(folder / f'{name}.png'), await asyncio.to_thread(frames.latest))
                print(f'  step {index + 1}: {step.press} x{step.repeat} ({step.note})')
            print(f'Running the sequence once (~{sequence_duration(config.sequence):.0f}s)...')
            await run_steps(controller, config.sequence, on_step=screenshot)
        print(f'Done. Screenshots after each step are in {folder}. If a step lands on the wrong '
              f'screen, change its "wait" or "repeat" in {CONFIG_FILE} and try again.')
    finally:
        await controller.close()


async def alert() -> None:
    if sys.platform == 'win32':
        import winsound

        for _ in range(5):
            await asyncio.to_thread(winsound.Beep, 1200, 300)
            await asyncio.sleep(0.2)
    else:
        print('\a')


async def cmd_hunt(config: Config) -> None:
    controller = await connect_controller(config)
    try:
        with FrameGrabber(config.camera, *config.frame_size) as frames:
            hunter = Hunter(controller, frames, config, OUTPUT_DIR)
            print(f'Hunting! Each reset takes ~{sequence_duration(config.sequence):.0f}s. '
                  'Press Ctrl+C to stop.')
            _frame, result = await hunter.run()
        print('\n' + '*' * 60)
        print(f'  SHINY FOUND after {hunter.stats.resets} resets! ({result})')
        print('  The bot has stopped pressing buttons. Check the screen, then catch/save it.')
        print('*' * 60)
        await alert()
        await asyncio.to_thread(input, 'Press Enter to disconnect the virtual controller...')
    except HuntStopped as stop:
        print(f'\nHunt stopped: {stop}')
    finally:
        await controller.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog='python -m shinybot', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['cameras', 'preview', 'setup', 'test-sequence', 'hunt'])
    parser.add_argument('--camera', type=int, help='capture device number (saved to the config)')
    args = parser.parse_args()

    setup_logging()
    config = Config.load()
    if args.camera is not None:
        config.camera = args.camera
    if not CONFIG_FILE.exists() or args.camera is not None:
        config.save()

    try:
        if args.command == 'cameras':
            cmd_cameras(config)
        elif args.command == 'preview':
            cmd_preview(config)
        elif args.command == 'setup':
            cmd_setup(config)
        elif args.command == 'test-sequence':
            asyncio.run(cmd_test_sequence(config))
        else:
            asyncio.run(cmd_hunt(config))
    except KeyboardInterrupt:
        print('\nStopped.')
    except RuntimeError as error:
        sys.exit(f'Error: {error}')


if __name__ == '__main__':
    main()
