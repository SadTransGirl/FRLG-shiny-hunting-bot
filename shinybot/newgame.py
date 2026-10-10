"""A frame-timed new game, so the secret ID (SID) can be worked out afterwards.

FireRed/LeafGreen make your trainer ID (TID) from a timer when you leave the naming screen,
then step the random number generator every frame until Oak's speech ends; the next value
is your SID. The game never shows the SID, but it only depends on the TID and how many
frames the speech took. So the bot plays the new game on a schedule worked out in the
emulator (sloop-emu-pc: python -m sloop.newgame plan), you read the TID off the trainer
card, and the plan turns it into a handful of possible SIDs.

The schedule is in frames after the title-screen press. Every press is timed from that one
moment (not from the previous press), so small delays don't add up over the minutes it runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from bt_controller.protocol import button_name

logger = logging.getLogger('shinybot.newgame')

PLAN_VERSION = 1
SOFT_RESET = 'A+B+PLUS+MINUS'
# Before the title-screen press (not timed to the frame): soft reset, skip the intro.
PRELUDE = [(SOFT_RESET, 0.5, 5.0), ('PLUS', 0.1, 5.0)]
MULT, INC = 0x41C64E6D, 0x6073


@dataclass
class Plan:
    name: str
    gender: str
    rival: str
    fps: float
    events: list[tuple[int, str, int]]   # (frame after the title press, buttons, frames held)
    data: dict

    @classmethod
    def load(cls, path: str | Path) -> Plan:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('version') != PLAN_VERSION:
            raise ValueError(f'{path}: not a version {PLAN_VERSION} new-game plan')
        events = [(int(f), str(b), int(h)) for f, b, h in data['events']]
        for _frame, buttons, _hold in events:
            for b in buttons.split('+'):
                button_name(b)  # unknown names fail here, not halfway through the run
        if any(b[0] <= a[0] for a, b in zip(events, events[1:])):
            raise ValueError(f'{path}: presses out of order')
        return cls(data['name'], data['gender'], data['rival'], float(data.get('fps', 60)), events, data)

    def duration(self) -> float:
        return sum(wait for _b, _h, wait in PRELUDE) + (self.events[-1][0] + 120) / self.fps


async def _sleep_until(loop: asyncio.AbstractEventLoop, when: float) -> None:
    """Sleep until loop time `when`: a normal sleep, then yield in a tight loop for the last
    couple of milliseconds (the Bluetooth reports keep going meanwhile)."""
    while True:
        left = when - loop.time()
        if left <= 0:
            return
        await asyncio.sleep(left - 0.002 if left > 0.003 else 0)


async def run_plan(controller, plan: Plan, on_progress=None, prelude=PRELUDE) -> list[float]:
    """Play the plan on the Switch. Returns how late each press was, in frames."""
    for buttons, hold, wait in prelude:
        await controller.press(*buttons.split('+'), duration=hold, wait=wait)
    loop = asyncio.get_running_loop()
    frame_time = 1 / plan.fps
    start = loop.time() + 0.05
    await _sleep_until(loop, start)
    controller.hold('PLUS')                       # frame 0: the title-screen press
    await _sleep_until(loop, start + 6 * frame_time)
    controller.release('PLUS')
    late = []
    for index, (frame, buttons, hold) in enumerate(plan.events):
        when = start + frame * frame_time
        await _sleep_until(loop, when)
        late.append((loop.time() - when) / frame_time)
        controller.hold(*buttons.split('+'))
        await _sleep_until(loop, when + hold * frame_time)
        controller.release(*buttons.split('+'))
        if on_progress:
            on_progress(index + 1, len(plan.events), frame)
    return late


# -- after the run: the SID ----------------------------------------------------------------
def rng_call(seed: int, n: int) -> int:
    """What the game's Random() returns on its n-th call after seeding."""
    mult, inc, state = MULT, INC, seed
    while n:
        if n & 1:
            state = (state * mult + inc) & 0xFFFFFFFF
        inc = (inc * (mult + 1)) & 0xFFFFFFFF
        mult = (mult * mult) & 0xFFFFFFFF
        n >>= 1
    return state >> 16


# The TID is a CPU-cycle count, so keystrokes landing a frame or two differently move it by
# tens to hundreds; pressing OK a frame later moves it by ~17,000-22,000. Match the nearest
# planned TID.
TID_TOLERANCE = 512


def tid_distance(a: int, b: int) -> int:
    d = (a - b) & 0xFFFF
    return min(d, 0x10000 - d)


def match_tid(table: dict[str, int], tid: int, tolerance: int = TID_TOLERANCE) -> int | None:
    near = [(tid_distance(int(t), tid), offset) for t, offset in table.items()]
    near = [n for n in near if n[0] <= tolerance]
    return min(near)[1] if near else None


def sid_candidates(plan: Plan, tid: int) -> tuple[list[tuple[int, int, str]], int | None]:
    """[(SID, RNG call, why)] most likely first, and how many frames off the bot was on the
    naming screen (None if the TID is near no planned one)."""
    data = plan.data
    offset = match_tid(data['tid_by_offset'], tid)
    naming = data['naming']['ok'] - data['naming']['open']
    after = data['speech_end'] - data['naming']['ok']
    ends = {int(e): c for e, c in data['calls_by_end_offset'].items()}
    rate = (ends[max(ends)] - ends[min(ends)]) / (max(ends) - min(ends)) if len(ends) > 1 else 1
    # The timing error measured on the naming screen, scaled to the length of the speech.
    drift = round((offset or 0) * after / naming)
    out, seen = [], set()
    for e in sorted(range(-3, 4), key=abs):
        calls = ends.get(drift + e, data['calls'] + round(rate * (drift + e)))
        sid = rng_call(tid, calls)
        if sid not in seen:
            seen.add(sid)
            out.append((sid, calls, f'speech ended {drift + e:+d} frames from the plan'))
    return out, offset


def describe_candidates(plan: Plan, tid: int) -> str:
    candidates, offset = sid_candidates(plan, tid)
    if offset is None:
        head = (f'TID {tid:05d} is not in the plan\'s table: either the Switch counts the naming '
                'screen differently from the emulator, or the bot was further off than the table '
                'covers. These SIDs assume the bot was on time:')
    elif offset == 0:
        head = f'TID {tid:05d}: the bot pressed OK exactly on the planned frame. Most likely first:'
    else:
        head = (f'TID {tid:05d}: the bot pressed OK {offset:+d} frames from the plan; the SIDs '
                'below allow for that. Most likely first:')
    lines = [head] + [f'  SID {sid:05d}   ({why})' for sid, _calls, why in candidates]
    return '\n'.join(lines)
