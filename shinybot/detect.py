"""Shiny detection by comparing screen regions against learned normal references.

No machine learning: the game draws the same sprite every reset, so a normal
Pokemon looks (almost) pixel-identical each time, while a shiny uses a different
palette and changes the colour of a large part of the sprite.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

Box = tuple[int, int, int, int]  # x, y, width, height

# A pixel counts as "changed" when a colour channel differs by at least this
# much (0-255), or more if the capture turns out to be noisier.
MIN_PIXEL_THRESHOLD = 20
MAX_PIXEL_THRESHOLD = 50
# Lowest thresholds, used when the capture is very clean. The real thresholds
# are set from how much normal calibration screenshots differ from each other:
# a clean capture catches even subtle shinies, a noisy one avoids false alarms.
# Fraction of changed pixels that marks a region as different:
MIN_CHANGED_FRACTION = 0.004
# Shift of a region's average colour (0-255) that marks it as different; catches
# palette swaps that per-pixel noise from video compression could hide:
MIN_COLOR_SHIFT = 0.8
# Calibration screenshots differing more than this mean the sequence isn't
# landing on the same screen every time (or one of them is already shiny).
CALIBRATION_MAX_FRACTION = 0.05
CALIBRATION_MAX_SHIFT = 4.0
# A screen box that changed less than this is still the summary screen (see check()).
MOSTLY_SAME_SCREEN = 0.5


def crop(frame: np.ndarray, box: Box) -> np.ndarray:
    x, y, w, h = box
    region = frame[y:y + h, x:x + w]
    if region.shape[0] != h or region.shape[1] != w:
        raise ValueError(f'box {box} is outside the {frame.shape[1]}x{frame.shape[0]} frame')
    return region


def _pixel_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Largest per-channel difference per pixel, after smoothing capture noise."""
    a = cv2.GaussianBlur(a, (5, 5), 0).astype(np.int16)
    b = cv2.GaussianBlur(b, (5, 5), 0).astype(np.int16)
    return np.abs(a - b).max(axis=2)


def _color_shift(a: np.ndarray, b: np.ndarray) -> float:
    """Largest per-channel difference between the regions' average colours."""
    return float(np.abs(a.reshape(-1, 3).mean(axis=0) - b.reshape(-1, 3).mean(axis=0)).max())


@dataclass
class RegionModel:
    """What a region normally looks like, and how much it varies between captures."""

    reference: np.ndarray
    pixel_threshold: float
    fraction_threshold: float
    shift_threshold: float
    calibration_fractions: list[float]
    calibration_shifts: list[float]

    @classmethod
    def learn(cls, samples: list[np.ndarray], sensitivity: float = 3.0,
              min_fraction: float = MIN_CHANGED_FRACTION, min_shift: float = MIN_COLOR_SHIFT
              ) -> RegionModel:
        if not samples:
            raise ValueError('need at least one calibration sample')
        reference = np.median(np.stack(samples), axis=0).astype(np.uint8)
        # Noise is measured between pairs of samples: two normal resets differ
        # about as much as two calibration screenshots do. (A sample that is
        # off entirely is caught by the calibration check instead.)
        pairs = [(a, b) for i, a in enumerate(samples) for b in samples[i + 1:]] or [(samples[0], samples[0])]
        pair_diffs = [_pixel_diff(a, b) for a, b in pairs]
        noise = float(np.median([np.percentile(d, 99.9) for d in pair_diffs]))
        pixel_threshold = min(MAX_PIXEL_THRESHOLD, max(MIN_PIXEL_THRESHOLD, noise * 1.5))
        pair_fraction = max(float((d >= pixel_threshold).mean()) for d in pair_diffs)
        pair_shift = max(_color_shift(a, b) for a, b in pairs)
        fraction_threshold = max(min_fraction, pair_fraction * sensitivity)
        shift_threshold = max(min_shift, pair_shift * sensitivity)
        diffs = [_pixel_diff(sample, reference) for sample in samples]
        fractions = [float((d >= pixel_threshold).mean()) for d in diffs]
        shifts = [_color_shift(sample, reference) for sample in samples]
        return cls(reference, pixel_threshold, fraction_threshold, shift_threshold, fractions, shifts)

    def changed_fraction(self, region: np.ndarray) -> float:
        return float((_pixel_diff(region, self.reference) >= self.pixel_threshold).mean())

    def color_shift(self, region: np.ndarray) -> float:
        return _color_shift(region, self.reference)

    def differs(self, region: np.ndarray) -> bool:
        return (self.changed_fraction(region) > self.fraction_threshold
                or self.color_shift(region) > self.shift_threshold)


@dataclass
class CheckResult:
    verdict: str  # 'normal', 'shiny' or 'wrong_screen'
    sprite_change: float
    sprite_shift: float
    screen_change: float

    def __str__(self) -> str:
        return (f'{self.verdict} (sprite: {self.sprite_change:.1%} of pixels changed, '
                f'colour shift {self.sprite_shift:.1f}; screen: {self.screen_change:.1%} changed)')


class ShinyDetector:
    """Learns the normal sprite from the first few resets, then flags differences."""

    def __init__(self, sprite_box: Box, screen_box: Box, sensitivity: float = 3.0) -> None:
        self.sprite_box = sprite_box
        self.screen_box = screen_box
        self.sensitivity = sensitivity
        self._sprite_samples: list[np.ndarray] = []
        self._screen_samples: list[np.ndarray] = []
        self.sprite: RegionModel | None = None
        self.screen: RegionModel | None = None

    @property
    def calibrated(self) -> bool:
        return self.sprite is not None

    def add_calibration_frame(self, frame: np.ndarray) -> None:
        self._sprite_samples.append(crop(frame, self.sprite_box).copy())
        self._screen_samples.append(crop(frame, self.screen_box).copy())

    def finish_calibration(self) -> list[str]:
        """Build the reference models; returns warnings if the samples disagree."""
        self.sprite = RegionModel.learn(self._sprite_samples, self.sensitivity)
        # The screen box only has to tell the summary screen from a different
        # screen, so it stays lenient (being picky there only stops hunts).
        self.screen = RegionModel.learn(self._screen_samples, self.sensitivity,
                                        CALIBRATION_MAX_FRACTION, CALIBRATION_MAX_SHIFT)
        warnings = []
        for name, model in (('sprite', self.sprite), ('screen', self.screen)):
            if (max(model.calibration_fractions) > CALIBRATION_MAX_FRACTION
                    or max(model.calibration_shifts) > CALIBRATION_MAX_SHIFT):
                warnings.append(
                    f'the calibration screenshots of the {name} box differ from each other '
                    f'({max(model.calibration_fractions):.1%} of pixels); check the saved '
                    'calibration images: the sequence may not reach the summary screen '
                    'every time, or one of them may already be shiny'
                )
        return warnings

    def check(self, frame: np.ndarray) -> CheckResult:
        if not self.calibrated:
            raise RuntimeError('calibrate first')
        screen = crop(frame, self.screen_box)
        sprite = crop(frame, self.sprite_box)
        sprite_change = self.sprite.changed_fraction(sprite)
        sprite_shift = self.sprite.color_shift(sprite)
        screen_change = self.screen.changed_fraction(screen)
        sprite_differs = self.sprite.differs(sprite)
        # A different screen changes most of the screen box. A small change there with a
        # changed sprite may be the shiny star (drawn beside the picture), so never throw
        # it away as a wrong screen: the confirmation frames and the person decide.
        if self.screen.differs(screen) and not (sprite_differs and screen_change < MOSTLY_SAME_SCREEN):
            verdict = 'wrong_screen'
        elif sprite_differs:
            verdict = 'shiny'
        else:
            verdict = 'normal'
        return CheckResult(verdict, sprite_change, sprite_shift, screen_change)
