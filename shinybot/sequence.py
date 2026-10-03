"""Button sequences, stored in the config file so timings can be tuned without code."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from bt_controller.protocol import button_name


@dataclass
class Step:
    press: str  # buttons joined with '+', e.g. 'A' or 'A+B+PLUS+MINUS'
    hold: float = 0.1  # seconds the buttons are held
    wait: float = 0.5  # seconds to wait after releasing
    repeat: int = 1
    note: str = ''

    @property
    def buttons(self) -> list[str]:
        return [button_name(b) for b in self.press.split('+')]

    @classmethod
    def from_dict(cls, data: dict) -> Step:
        step = cls(**data)
        step.buttons  # validate button names early
        if step.repeat < 1 or step.hold < 0 or step.wait < 0:
            raise ValueError(f'invalid step {data}')
        return step


# Starter soft reset for FireRed/LeafGreen. Before starting: text speed FAST,
# saved in Oak's lab standing in front of the chosen Poke Ball, facing it.
STARTER_SEQUENCE = [
    Step('A+B+PLUS+MINUS', hold=0.5, wait=3.0, note='soft reset'),
    Step('PLUS', wait=3.0, note='skip intro to title screen'),
    Step('PLUS', wait=2.5, note='title screen to main menu'),
    Step('A', wait=3.5, note='CONTINUE'),
    Step('B', wait=1.5, note='skip "previously on your quest" recap'),
    # 3 x A covers 0-2 text boxes before the YES/NO question but stops before
    # the nickname question; B then advances text and answers that with NO.
    Step('A', wait=1.5, repeat=3, note='choose the Poke Ball, answer YES'),
    Step('B', wait=0.6, repeat=15, note='receive it, decline nickname, rival dialogue'),
    Step('PLUS', wait=1.0, note='open menu'),
    Step('A', wait=1.5, note='POKEMON'),
    Step('A', wait=1.0, note='first Pokemon'),
    Step('A', wait=2.5, note='SUMMARY'),
]


def steps_from_config(data: list[dict]) -> list[Step]:
    return [Step.from_dict(item) for item in data]


def steps_to_config(steps: list[Step]) -> list[dict]:
    return [asdict(step) for step in steps]


async def run_steps(
    controller,
    steps: list[Step],
    on_step: Callable[[int, Step], Awaitable[None]] | None = None,
) -> None:
    """Press each step's buttons; `on_step` runs after every step (e.g. to screenshot)."""
    for index, step in enumerate(steps):
        for _ in range(step.repeat):
            await controller.press(*step.buttons, duration=step.hold, wait=step.wait)
        if on_step:
            await on_step(index, step)


def sequence_duration(steps: list[Step]) -> float:
    return sum((s.hold + s.wait) * s.repeat for s in steps)
