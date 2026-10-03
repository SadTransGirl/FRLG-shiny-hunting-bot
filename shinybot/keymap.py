"""Turn held keys / clicked pad buttons into controller state."""

from __future__ import annotations

from bt_controller.protocol import button_name

STICK_DIRECTIONS = {
    'LS_UP': (0, 1), 'LS_DOWN': (0, -1), 'LS_LEFT': (-1, 0), 'LS_RIGHT': (1, 0),
}


class InputState:
    """Tracks what is held, from any number of sources (keys, mouse clicks).

    A button stays held while at least one source holds it.
    """

    def __init__(self, key_map: dict[str, str]) -> None:
        self.key_map = {}
        for key, target in key_map.items():
            target = target.upper()
            if target not in STICK_DIRECTIONS:
                target = button_name(target)
            self.key_map[key.lower()] = target
        self._held: dict[str, str] = {}  # source -> button or stick direction

    def press(self, source: str, target: str) -> bool:
        """Returns True if the resulting state changed."""
        before = self.snapshot()
        self._held[source] = target
        return self.snapshot() != before

    def release(self, source: str) -> bool:
        before = self.snapshot()
        self._held.pop(source, None)
        return self.snapshot() != before

    def clear(self) -> bool:
        changed = bool(self._held)
        self._held.clear()
        return changed

    def buttons(self) -> set[str]:
        return {t for t in self._held.values() if t not in STICK_DIRECTIONS}

    def stick(self) -> tuple[float, float]:
        x = y = 0
        for target in set(self._held.values()):
            dx, dy = STICK_DIRECTIONS.get(target, (0, 0))
            x += dx
            y += dy
        return float(max(-1, min(1, x))), float(max(-1, min(1, y)))

    def snapshot(self) -> tuple[frozenset[str], tuple[float, float]]:
        return frozenset(self.buttons()), self.stick()


def apply_to_controller(controller, buttons: set[str], stick: tuple[float, float]) -> None:
    """Make the controller hold exactly `buttons` (run on the controller's event loop)."""
    controller.release()
    if buttons:
        controller.hold(*buttons)
    controller.set_stick('left', *stick)
