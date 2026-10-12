"""Seed check: is every soft reset really giving a new starter?

The bot can't read the Switch's RNG, but it can see what the RNG makes. The summary screen
shows the starter's nature and gender, so two resets that end on the same seed and frame show
exactly the same screen, while two different starters match only when the nature and gender
happen to agree: 1/25 x (7/8 x 7/8 + 1/8 x 1/8) = about 3% of the time (starters are 7/8 male).
If the random waits stopped working, nearly every reset would match the one before.

Each reset is compared with the previous one outside the sprite box (a shiny changes the
sprite, nothing else does), at a reduced size so capture noise averages out; a second frame of
the same screen measures the noise that's left.
"""

from __future__ import annotations

import csv
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

WIDTH = 400              # px the frame is reduced to (about the GBA's own size in a 16:9 capture)
LEVEL = 24               # gray levels a pixel must change by to count
MIN_CHANGED = 20         # fewer changed pixels than this (or than 3x the noise) = the same screen
NOISE_FACTOR = 3
EXPECTED_SAME = 1 / 25 * (7 / 8 * 7 / 8 + 1 / 8 * 1 / 8)   # different starters, same screen
WINDOW = 100             # resets the summary covers
WARN_AFTER = 30          # resets before judging
WARN_SAME = 0.25         # this share of "same as last" means the seeds aren't changing


def signature(frame: np.ndarray, sprite_box) -> np.ndarray:
    """The frame in gray at WIDTH px wide, with the sprite box blanked."""
    height, width = frame.shape[:2]
    scale = WIDTH / width
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    small = cv2.resize(gray, (WIDTH, max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    if sprite_box:
        x, y, w, h = (round(v * scale) for v in sprite_box)
        small[max(0, y - 1):y + h + 1, max(0, x - 1):x + w + 1] = 0
    return small


def changed_pixels(a: np.ndarray, b: np.ndarray) -> int:
    if a.shape != b.shape:
        return a.size
    return int(np.count_nonzero(cv2.absdiff(a, b) > LEVEL))


@dataclass
class Comparison:
    changed: int      # pixels that differ from the previous reset's screen
    noise: int        # pixels that differ between two frames of this reset's screen
    same: bool | None  # same screen as the previous reset (None: nothing to compare with)


class SeedCheck:
    """Compares each reset's summary screen with the previous one and keeps the tally."""

    def __init__(self, sprite_box, log_path: Path | None = None) -> None:
        self.sprite_box = sprite_box
        self.log_path = log_path
        self.previous: np.ndarray | None = None
        self.recent: deque[bool] = deque(maxlen=WINDOW)
        self.warned = False

    def add(self, frame: np.ndarray, again: np.ndarray | None = None) -> Comparison:
        """`frame` is this reset's summary screen, `again` a second frame of it (for the noise)."""
        current = signature(frame, self.sprite_box)
        noise = changed_pixels(current, signature(again, self.sprite_box)) if again is not None else 0
        if self.previous is None:
            result = Comparison(0, noise, None)
        else:
            changed = changed_pixels(current, self.previous)
            result = Comparison(changed, noise, changed < max(MIN_CHANGED, NOISE_FACTOR * noise))
            self.recent.append(result.same)
        self.previous = current
        return result

    @property
    def same_share(self) -> float | None:
        return sum(self.recent) / len(self.recent) if self.recent else None

    def summary(self) -> str:
        if not self.recent:
            return 'seed check: not enough resets yet'
        new = len(self.recent) - sum(self.recent)
        return (f'seed check: {new} of the last {len(self.recent)} resets showed a different starter '
                f'from the one before (about {100 * (1 - EXPECTED_SAME):.0f}% expected when every reset '
                f'gets a new seed)')

    def verdict(self) -> str | None:
        """A warning when the starters repeat far more than chance allows, else None."""
        share = self.same_share
        if share is None or len(self.recent) < WARN_AFTER or share < WARN_SAME:
            return None
        return (f'seed check: {100 * share:.0f}% of the last {len(self.recent)} resets ended on the same '
                f'summary screen as the reset before (about {100 * EXPECTED_SAME:.0f}% is normal). The '
                'resets seem to be landing on the same few seeds: check the sequence still has its '
                'random waits ("Random +") and that the presses reach the Switch.')

    def record(self, reset: int, waits: list[float], verdict: str, result: Comparison) -> None:
        """Append a row to the CSV log (one per reset), for checking the spread later."""
        if self.log_path is None:
            return
        new = not self.log_path.exists()
        try:
            with self.log_path.open('a', newline='', encoding='utf-8') as f:
                out = csv.writer(f)
                if new:
                    out.writerow(['reset', 'time', 'random_waits_s', 'verdict', 'same_as_last',
                                  'changed_px', 'noise_px'])
                out.writerow([reset, time.strftime('%Y-%m-%d %H:%M:%S'), ' '.join(f'{w:.3f}' for w in waits),
                              verdict, '' if result.same is None else int(result.same), result.changed,
                              result.noise])
        except OSError as error:
            logger.debug('could not write %s: %s', self.log_path, error)
