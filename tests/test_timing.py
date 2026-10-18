"""The timing test: fixed schedule, exact-time presses, and repeatable vs random starters."""
import asyncio
import random
import time

import pytest

from shinybot.sequence import Step
from shinybot.timing import StarterGroups, fixed_schedule, run_schedule, schedule_end, timing_test
from test_variety import NATURES, SPRITE_BOX, summary_screen


class HoldController:
    def __init__(self):
        self.events = []

    def hold(self, *buttons):
        self.events.append(('hold', '+'.join(buttons), time.monotonic()))

    def release(self, *buttons):
        self.events.append(('release', '+'.join(buttons), time.monotonic()))


class ScreenFrames:
    def __init__(self, screens):
        self.screens = list(screens)

    def latest_pair(self):
        nature, male, seed = self.screens.pop(0)
        return summary_screen(nature, male, seed=seed), summary_screen(nature, male, seed=seed + 1)


def test_fixed_schedule_puts_random_waits_at_their_middle():
    steps = [Step('A+B+PLUS+MINUS', hold=0.5, wait=5.0), Step('PLUS', hold=0.1, wait=5.0, random_wait=5.0),
             Step('A', hold=0.1, wait=1.5, repeat=2)]
    assert fixed_schedule(steps) == [(0.0, 'A+B+PLUS+MINUS', 0.5), (5.5, 'PLUS', 0.1),
                                     (pytest.approx(13.1), 'A', 0.1), (pytest.approx(14.7), 'A', 0.1)]
    assert schedule_end(steps) == pytest.approx(16.3)


def test_presses_land_at_their_times():
    controller = HoldController()
    schedule = [(0.0, 'A', 0.02), (0.1, 'B', 0.02), (0.25, 'DOWN', 0.02)]
    late = asyncio.run(run_schedule(controller, schedule, end=0.3))
    holds = [(b, t) for kind, b, t in controller.events if kind == 'hold']
    assert [b for b, _t in holds] == ['A', 'B', 'DOWN']
    start = holds[0][1]
    assert [round(t - start, 2) for _b, t in holds] == [0.0, 0.1, 0.25]
    assert max(late) < 0.01


def test_same_few_starters_is_repeatable():
    groups = StarterGroups(SPRITE_BOX)
    for i in range(24):
        nature, male = [('BOLD', True), ('CALM', True), ('BOLD', False)][i % 3]
        groups.add(summary_screen(nature, male, seed=2 * i), summary_screen(nature, male, seed=2 * i + 1))
    assert groups.counts == [8, 8, 8]
    kind, text = groups.verdict()
    assert kind == 'repeatable' and '3 different starter screens' in text


def test_random_starters_are_not_repeatable():
    groups = StarterGroups(SPRITE_BOX)
    rng = random.Random(3)
    for i in range(24):
        nature, male = rng.choice(NATURES), rng.random() < 7 / 8
        groups.add(summary_screen(nature, male, seed=2 * i), summary_screen(nature, male, seed=2 * i + 1))
    kind, _text = groups.verdict()
    assert kind == 'random' and len(groups.counts) >= 12


def test_timing_test_runs_the_sequence_and_saves_each_starter(tmp_path):
    steps = [Step('A+B+PLUS+MINUS', hold=0.01, wait=0.01), Step('A', hold=0.01, wait=0.01, random_wait=0.02)]
    frames = ScreenFrames([('BOLD', True, 0), ('CALM', True, 2), ('BOLD', True, 4), ('BOLD', True, 6)])
    controller = HoldController()
    groups = asyncio.run(timing_test(controller, frames, steps, SPRITE_BOX, tmp_path, resets=4))
    assert groups.counts == [3, 1] and groups.sequence == [0, 1, 0, 0]
    assert sorted(p.name for p in tmp_path.glob('timing_starter_*.png')) == ['timing_starter_1.png',
                                                                           'timing_starter_2.png']
    assert sum(1 for kind, *_ in controller.events if kind == 'hold') == 8
