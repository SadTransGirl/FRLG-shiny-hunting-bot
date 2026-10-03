"""Settings stored in shinybot.json next to where the bot is run."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .sequence import STARTER_SEQUENCE, Step, steps_from_config, steps_to_config

CONFIG_FILE = Path('shinybot.json')
DEFAULT_SEQUENCE_NAME = 'Starter'

# Keyboard -> controller, used by the window. Keys are Tk key names in lower
# case; LS_* move the left stick.
DEFAULT_KEYS = {
    'up': 'UP', 'down': 'DOWN', 'left': 'LEFT', 'right': 'RIGHT',
    'x': 'A', 'z': 'B', 's': 'X', 'a': 'Y',
    'q': 'L', 'w': 'R', '1': 'ZL', '2': 'ZR',
    'return': 'PLUS', 'backspace': 'MINUS', 'h': 'HOME', 'c': 'CAPTURE',
    'i': 'LS_UP', 'k': 'LS_DOWN', 'j': 'LS_LEFT', 'l': 'LS_RIGHT',
}


@dataclass
class Config:
    camera: int = 0
    frame_size: tuple[int, int] = (1920, 1080)
    transport: str = 'usb:0'
    # Regions in frame pixels (x, y, width, height); set with "python -m shinybot setup".
    sprite_box: tuple[int, int, int, int] | None = None
    screen_box: tuple[int, int, int, int] | None = None
    calibration_resets: int = 3
    sensitivity: float = 3.0
    max_wrong_screens: int = 3  # stop after this many failed resets in a row
    save_every_encounter: bool = False
    # Named button sequences (recorded or hand-written); hunts use the active one.
    sequences: dict[str, list[Step]] = field(
        default_factory=lambda: {DEFAULT_SEQUENCE_NAME: copy.deepcopy(STARTER_SEQUENCE)})
    active_sequence: str = DEFAULT_SEQUENCE_NAME
    keys: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KEYS))

    @property
    def sequence(self) -> list[Step]:
        if self.active_sequence in self.sequences:
            return self.sequences[self.active_sequence]
        return next(iter(self.sequences.values()), [])

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> Config:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding='utf-8'))
        if 'sequence' in data:  # older files had a single sequence
            data.setdefault('sequences', {DEFAULT_SEQUENCE_NAME: data.pop('sequence')})
            data.pop('sequence', None)
        if 'sequences' in data:
            data['sequences'] = {name: steps_from_config(steps)
                                 for name, steps in data['sequences'].items()}
        for key in ('frame_size', 'sprite_box', 'screen_box'):
            if data.get(key) is not None:
                data[key] = tuple(data[key])
        config = cls(**data)
        if config.active_sequence not in config.sequences and config.sequences:
            config.active_sequence = next(iter(config.sequences))
        return config

    def save(self, path: Path = CONFIG_FILE) -> None:
        data = asdict(self)
        data['sequences'] = {name: steps_to_config(steps) for name, steps in self.sequences.items()}
        path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
