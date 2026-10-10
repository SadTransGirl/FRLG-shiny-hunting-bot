import asyncio
import json

import pytest

from shinybot.newgame import Plan, describe_candidates, rng_call, run_plan, sid_candidates


def plan_data(**changes):
    data = {
        'version': 1, 'name': 'Naomi', 'gender': 'girl', 'rival': 'RED', 'fps': 60,
        'events': [[30, 'A', 6], [60, 'DOWN', 6], [90, 'A+B', 3]],
        'naming': {'open': 1000, 'ok': 1900}, 'speech_end': 3700,
        'tid': 61050, 'sid': 16480, 'calls': 1900,
        'tid_by_offset': {'61050': 0, '1234': 2},
        'calls_by_end_offset': {'-2': 1896, '-1': 1898, '0': 1900, '1': 1902, '2': 1904},
    }
    data.update(changes)
    return data


def write_plan(tmp_path, **changes):
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(plan_data(**changes)))
    return path


def test_rng_matches_the_game():
    # a new game simulated in the emulator: TID 61050, SID 16480 on call 1900
    assert rng_call(61050, 1900) == 16480
    assert rng_call(37527, 2783) == 14519


def test_plan_loading_checks_the_file(tmp_path):
    plan = Plan.load(write_plan(tmp_path))
    assert plan.events[2] == (90, 'A+B', 3) and plan.fps == 60
    for bad in ({'version': 2}, {'events': [[30, 'TURBO', 6]]}, {'events': [[60, 'A', 6], [30, 'B', 6]]}):
        with pytest.raises(ValueError):
            Plan.load(write_plan(tmp_path, **bad))


def test_sid_candidates():
    plan = Plan('Naomi', 'girl', 'RED', 60, [], plan_data())
    on_time, offset = sid_candidates(plan, 61050)
    assert offset == 0 and on_time[0][:2] == (16480, 1900)
    assert [c for _s, c, _w in on_time[:3]] == [1900, 1898, 1902]  # nearest first
    late, offset = sid_candidates(plan, 1234)  # OK pressed 2 frames late over 900 frames
    assert offset == 2 and late[0][1] == 1900 + 2 * 4  # 2 x 1800/900 = 4 frames, 2 calls each
    assert 'not in the plan' in describe_candidates(plan, 999)


class TimingController:
    def __init__(self):
        self.log = []

    def hold(self, *buttons):
        self.log.append(('hold', buttons, asyncio.get_running_loop().time()))

    def release(self, *buttons):
        self.log.append(('release', buttons, asyncio.get_running_loop().time()))

    async def press(self, *buttons, duration=0.1, wait=0.05):
        self.log.append(('press', buttons, asyncio.get_running_loop().time()))


async def test_run_plan_times_every_press_from_the_title_press(tmp_path):
    plan = Plan.load(write_plan(tmp_path, fps=300))  # 5x speed so the test is quick
    controller = TimingController()
    late = await run_plan(controller, plan, prelude=[])
    holds = [(b, t) for kind, b, t in controller.log if kind == 'hold']
    assert [b for b, _t in holds] == [('PLUS',), ('A',), ('DOWN',), ('A', 'B')]
    start = holds[0][1]
    for (buttons, t), frame in zip(holds[1:], (30, 60, 90)):
        assert abs((t - start) - frame / 300) < 0.01, buttons
    assert len(late) == 3 and max(late) < 3
    releases = [b for kind, b, _t in controller.log if kind == 'release']
    assert releases == [('PLUS',), ('A',), ('DOWN',), ('A', 'B')]
