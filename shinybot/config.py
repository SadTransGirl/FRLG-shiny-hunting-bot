"""Settings stored in shinybot.json next to where the bot is run."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .sequence import STARTER_SEQUENCE, Step, steps_from_config, steps_to_config

CONFIG_FILE = Path('shinybot.json')


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
    sequence: list[Step] = field(default_factory=lambda: list(STARTER_SEQUENCE))

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> Config:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding='utf-8'))
        data['sequence'] = steps_from_config(data.get('sequence', steps_to_config(STARTER_SEQUENCE)))
        for key in ('frame_size', 'sprite_box', 'screen_box'):
            if data.get(key) is not None:
                data[key] = tuple(data[key])
        return cls(**data)

    def save(self, path: Path = CONFIG_FILE) -> None:
        data = asdict(self)
        data['sequence'] = steps_to_config(self.sequence)
        path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
