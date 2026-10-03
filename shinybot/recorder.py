"""Record manual play into sequence steps (press, hold, wait)."""

from __future__ import annotations

import time
from collections.abc import Callable

from .sequence import Step

MIN_HOLD = 0.05
# Order buttons are written in, e.g. 'A+B+PLUS+MINUS'.
BUTTON_ORDER = ['A', 'B', 'X', 'Y', 'L', 'R', 'ZL', 'ZR', 'PLUS', 'MINUS', 'HOME', 'CAPTURE',
                'UP', 'DOWN', 'LEFT', 'RIGHT', 'LSTICK', 'RSTICK']


def _round(seconds: float) -> float:
    return round(round(max(0.0, seconds) / 0.05) * 0.05, 2)


def chord_name(buttons: set[str]) -> str:
    """'A+B+PLUS+MINUS' in a stable order."""
    return '+'.join(name for name in BUTTON_ORDER if name in buttons)


class Recorder:
    """Turns a stream of "these buttons are held now" updates into Steps.

    A step starts when the first button goes down and ends when every button is
    released; buttons pressed meanwhile join the step as a chord. Each step's
    wait is the gap until the next press (for the last step: until stop()).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.steps: list[Step] = []
        self._press_start: float | None = None
        self._chord: set[str] = set()
        self._last_release: float | None = None

    @property
    def recording_press(self) -> bool:
        return self._press_start is not None

    def update(self, held: set[str]) -> None:
        now = self._clock()
        if held and self._press_start is None:
            if self.steps and self._last_release is not None:
                self.steps[-1].wait = _round(now - self._last_release)
            self._press_start = now
            self._chord = set(held)
        elif held:
            self._chord |= held
        elif self._press_start is not None:
            self._finish_press(now)

    def _finish_press(self, now: float) -> None:
        hold = max(MIN_HOLD, _round(now - self._press_start))
        self.steps.append(Step(chord_name(self._chord), hold=hold, wait=0.0))
        self._last_release = now
        self._press_start = None
        self._chord = set()

    def stop(self) -> list[Step]:
        now = self._clock()
        if self._press_start is not None:
            self._finish_press(now)
        if self.steps and self._last_release is not None:
            self.steps[-1].wait = _round(now - self._last_release)
        return self.steps


def merge_repeats(steps: list[Step], tolerance: float = 0.35) -> list[Step]:
    """Combine runs of the same press (e.g. mashing B) into one step with `repeat`.

    Steps merge when their buttons match and their waits differ by at most
    `tolerance` seconds. The merged step uses the longest hold and wait, so the
    replay is never faster than what was recorded. Notes are kept (joined).
    """
    merged: list[Step] = []
    for step in steps:
        last = merged[-1] if merged else None
        if (last is not None and last.press == step.press
                and abs(last.wait - step.wait) <= tolerance
                and abs(last.hold - step.hold) <= tolerance):
            last.repeat += step.repeat
            last.hold = max(last.hold, step.hold)
            last.wait = max(last.wait, step.wait)
            if step.note and step.note not in last.note:
                last.note = f'{last.note}; {step.note}' if last.note else step.note
        else:
            merged.append(Step(step.press, step.hold, step.wait, step.repeat, step.note))
    return merged
