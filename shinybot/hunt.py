"""The soft-reset loop: run the sequence, screenshot, check, repeat until shiny."""

from __future__ import annotations

import asyncio
import json
import contextlib
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import Config
from .detect import (CALIBRATION_MAX_FRACTION, CALIBRATION_MAX_SHIFT, CheckResult, RegionModel,
                     ShinyDetector, crop)
from .sequence import has_random_wait, run_steps

logger = logging.getLogger(__name__)

SETUP_FRAME = 'setup_frame.png'
CONFIRM_FRAMES = 2  # extra frames that must also look shiny before the hunt stops
NO_RANDOM_WAIT = (
    'The sequence has no random wait. FireRed/LeafGreen pick the starter from exact frame '
    'timing, so with the same timing every reset you get the same few starters over and over '
    'and may never see a shiny. In the sequence editor, set "Random +" to about 3 s on the '
    'step that waits on the title screen and on a step where you stand in the lab before '
    'taking the Poke Ball (the default Starter sequence does this).'
)


@contextlib.contextmanager
def keep_pc_awake():
    """Stop Windows from sleeping while hunting (the display may still turn off)."""
    if sys.platform != 'win32':
        yield
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
    try:
        yield
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous)


class HuntStopped(Exception):
    """The hunt can't safely continue; the message says why."""


@dataclass
class Stats:
    resets: int = 0
    shinies: int = 0

    @classmethod
    def load(cls, path: Path) -> Stats:
        try:
            return cls(**json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.__dict__) + '\n', encoding='utf-8')


class Hunter:
    def __init__(self, controller, frames, config: Config, output_dir: Path) -> None:
        if not config.sprite_box or not config.screen_box:
            raise HuntStopped('no screen regions set yet; run "python -m shinybot setup" first')
        self.controller = controller
        self.frames = frames
        self.config = config
        self.output_dir = output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        self.stats_path = output_dir / 'stats.json'
        self.stats = Stats.load(self.stats_path)
        self.detector = ShinyDetector(config.sprite_box, config.screen_box, config.sensitivity)
        self.expected_screen = self._load_expected_screen()
        self.last_result: CheckResult | None = None

    def _load_expected_screen(self) -> RegionModel | None:
        setup = cv2.imread(str(self.output_dir / SETUP_FRAME))
        if setup is None:
            return None
        return RegionModel.learn([crop(setup, self.config.screen_box)],
                                 min_fraction=CALIBRATION_MAX_FRACTION, min_shift=CALIBRATION_MAX_SHIFT)

    def _save(self, name: str, frame: np.ndarray) -> Path:
        path = self.output_dir / name
        cv2.imwrite(str(path), frame)
        return path

    async def _ensure_connected(self) -> None:
        while not self.controller.connected:
            logger.warning('controller not connected; reconnecting...')
            try:
                await self.controller.reconnect()
            except Exception as error:  # Switch asleep, out of range, ...
                logger.warning('reconnect failed (%s); retrying in 10 s', error)
                await asyncio.sleep(10)

    async def attempt(self) -> np.ndarray:
        """One soft reset through to the summary screen; returns the screenshot."""
        await self._ensure_connected()
        await run_steps(self.controller, self.config.sequence)
        frame = await asyncio.to_thread(self.frames.latest)
        self.stats.resets += 1
        self.stats.save(self.stats_path)
        self._save('last_check.png', frame)
        return frame

    async def calibrate(self) -> None:
        count = self.config.calibration_resets
        logger.info('calibrating: learning what a normal %d resets look like', count)
        for index in range(count):
            frame = await self.attempt()
            path = self._save(f'calibration_{index + 1}.png', frame)
            if self.expected_screen and self.expected_screen.differs(crop(frame, self.config.screen_box)):
                raise HuntStopped(
                    f'calibration reset {index + 1} did not end on the screen chosen in setup '
                    f'(see {path}). Run "python -m shinybot test-sequence" and adjust the timings.'
                )
            self.detector.add_calibration_frame(frame)
            logger.info('calibration %d/%d saved to %s', index + 1, count, path)
        warnings = self.detector.finish_calibration()
        if warnings:
            raise HuntStopped('calibration looks unreliable: ' + '; '.join(warnings))
        logger.info('calibration done')

    async def _confirm_shiny(self, frame: np.ndarray) -> bool:
        """A shiny stays shiny in every frame; capture noise doesn't. Check a few more."""
        for _ in range(CONFIRM_FRAMES):
            await asyncio.sleep(0.25)
            again = self.detector.check(await asyncio.to_thread(self.frames.latest))
            if again.verdict != 'shiny':
                path = self._save(f'unconfirmed_{self.stats.resets}.png', frame)
                logger.info('possible shiny not confirmed by the next frame (%s); probably capture '
                            'noise, continuing. Screenshot: %s', again, path)
                return False
        return True

    async def run(self) -> tuple[np.ndarray, CheckResult]:
        """Hunt until a shiny is found; returns its screenshot and check result."""
        if not has_random_wait(self.config.sequence):
            logger.warning(NO_RANDOM_WAIT)
        with keep_pc_awake():
            return await self._run()

    async def _run(self) -> tuple[np.ndarray, CheckResult]:
        await self.calibrate()
        wrong_in_a_row = 0
        started = time.monotonic()
        session_resets = 0
        while True:
            frame = await self.attempt()
            session_resets += 1
            result = self.detector.check(frame)
            self.last_result = result
            rate = session_resets / max(1e-9, time.monotonic() - started) * 3600
            logger.info('reset %d: %s [%.0f resets/hour]', self.stats.resets, result, rate)
            if self.config.save_every_encounter:
                self._save(f'encounter_{self.stats.resets:06d}.png', frame)

            if result.verdict == 'shiny' and not await self._confirm_shiny(frame):
                continue

            if result.verdict == 'shiny':
                self.stats.shinies += 1
                self.stats.save(self.stats_path)
                path = self._save(f'SHINY_reset_{self.stats.resets}.png', frame)
                logger.info('SHINY candidate after %d resets! Screenshot: %s', self.stats.resets, path)
                return frame, result

            if result.verdict == 'wrong_screen':
                wrong_in_a_row += 1
                path = self._save(f'wrong_screen_{self.stats.resets}.png', frame)
                logger.warning('reset did not reach the summary screen (%s)', path)
                if wrong_in_a_row >= self.config.max_wrong_screens:
                    raise HuntStopped(
                        f'{wrong_in_a_row} resets in a row missed the summary screen. '
                        'Run "python -m shinybot test-sequence" to see where it goes wrong.'
                    )
            else:
                wrong_in_a_row = 0
