"""Timing test: does the Switch give the same starter for the same button timing?

Aiming at a planned shiny only works if it does. The test plays the active sequence with
every random wait fixed at its middle and every press at an exact time after the soft reset
(as the timed new game does), and groups the summary screens it ends on (nature and gender
show there; see variety.py).

  * Repeatable: a few starters over and over. The bot's clock and the Switch's frames aren't
    in step, so each reset can land a frame either side, but the same few frames come back.
    Aiming at a planned frame can work.
  * Not repeatable: nearly every reset different (30 random starters show about 20 different
    screens). Something on the Switch varies the seed by itself; aiming can't work, and the
    normal hunt is the way.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .detect import crop
from .newgame import _sleep_until
from .sequence import Step
from .variety import MIN_CHANGED, NOISE_FACTOR, changed_pixels, signature

logger = logging.getLogger(__name__)

REPEATABLE_SHARE = 0.25     # groups <= this share of resets: repeatable
RANDOM_SHARE = 0.5          # groups >= this share: not repeatable


def fixed_schedule(steps: list[Step]) -> list[tuple[float, str, float]]:
    """[(seconds after the first press, buttons, hold seconds)]: the sequence with each random
    wait fixed at its middle, so every run presses at the same times."""
    schedule, now = [], 0.0
    for step in steps:
        for _ in range(step.repeat):
            schedule.append((now, step.press, step.hold))
            now += step.hold + step.wait
        now += step.random_wait / 2
    return schedule


def schedule_end(steps: list[Step]) -> float:
    """When the last step's wait is over (the summary screen should be up)."""
    return sum((s.hold + s.wait) * s.repeat + s.random_wait / 2 for s in steps)


async def run_schedule(controller, schedule: list[tuple[float, str, float]], end: float) -> list[float]:
    """Press every button at its time after the first press. Returns how late each was (s)."""
    loop = asyncio.get_running_loop()
    start = loop.time() + 0.05
    late = []
    for at, buttons, hold in schedule:
        when = start + at
        await _sleep_until(loop, when)
        late.append(loop.time() - when)
        controller.hold(*buttons.split('+'))
        await _sleep_until(loop, when + hold)
        controller.release(*buttons.split('+'))
    await _sleep_until(loop, start + end)
    return late


@dataclass
class StarterGroups:
    """Summary screens grouped by what they show (same starter = same screen)."""
    sprite_box: tuple | None
    signatures: list[np.ndarray] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)
    sequence: list[int] = field(default_factory=list)    # group of each reset, in order

    def add(self, frame: np.ndarray, again: np.ndarray | None = None) -> tuple[int, bool]:
        """Put a screen in its group; returns (group index, new group?)."""
        current = signature(frame, self.sprite_box)
        noise = changed_pixels(current, signature(again, self.sprite_box)) if again is not None else 0
        limit = max(MIN_CHANGED, NOISE_FACTOR * noise)
        for index, known in enumerate(self.signatures):
            if changed_pixels(current, known) < limit:
                self.counts[index] += 1
                self.sequence.append(index)
                return index, False
        self.signatures.append(current)
        self.counts.append(1)
        self.sequence.append(len(self.counts) - 1)
        return len(self.counts) - 1, True

    @property
    def resets(self) -> int:
        return sum(self.counts)

    def verdict(self) -> tuple[str, str]:
        """('repeatable' | 'random' | 'unclear', explanation)."""
        resets, groups = self.resets, len(self.counts)
        counts = ', '.join(str(c) for c in sorted(self.counts, reverse=True))
        head = f'{resets} resets with the same timing gave {groups} different starter screens ({counts}).'
        if resets < 8:
            return 'unclear', head + ' Too few resets to tell: run at least 20.'
        if groups <= max(2, REPEATABLE_SHARE * resets):
            return 'repeatable', head + (
                ' The Switch repeats: the same timing gives the same few starters, so aiming the '
                'bot at a planned shiny can work.')
        if groups >= RANDOM_SHARE * resets:
            return 'random', head + (
                ' Nearly every reset was different although the timing was the same: something on '
                'the Switch moves the seed by itself, so aiming at a planned shiny can\'t work. '
                'The normal hunt (random waits) is the way.')
        return 'unclear', head + (
            ' In between: some repeats, but more spread than a frame of timing explains. Run it '
            'again with more resets.')


async def timing_test(controller, frames, steps: list[Step], sprite_box, output_dir: Path,
                      resets: int = 30, expected_screen=None, screen_box=None) -> StarterGroups:
    """Play the sequence with fixed timing `resets` times and group the starters."""
    output_dir.mkdir(parents=True, exist_ok=True)
    schedule, end = fixed_schedule(steps), schedule_end(steps)
    groups = StarterGroups(sprite_box)
    for number in range(1, resets + 1):
        late = await run_schedule(controller, schedule, end)
        frame, again = await asyncio.to_thread(frames.latest_pair)
        if expected_screen is not None and screen_box and expected_screen.differs(crop(frame, screen_box)):
            path = output_dir / f'timing_wrong_screen_{number}.png'
            cv2.imwrite(str(path), frame)
            logger.warning('timing test %d/%d: not on the summary screen (%s); left out', number, resets, path)
            continue
        group, new = groups.add(frame, again)
        if new:
            cv2.imwrite(str(output_dir / f'timing_starter_{group + 1}.png'), frame)
        logger.info('timing test %d/%d: starter #%d (seen %d times; presses up to %.1f ms late)', number,
                    resets, group + 1, groups.counts[group], 1000 * max(late))
    kind, text = groups.verdict()
    logger.info('timing test: %s Pictures of each starter: %s', text, output_dir / 'timing_starter_*.png')
    return groups
