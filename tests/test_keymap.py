import pytest

from shinybot.config import DEFAULT_KEYS
from shinybot.keymap import InputState, apply_to_controller


def test_key_map_normalises_names():
    state = InputState({'X': 'a', 'Return': 'start', 'i': 'ls_up'})
    assert state.key_map == {'x': 'A', 'return': 'PLUS', 'i': 'LS_UP'}


def test_default_keys_are_valid():
    InputState(DEFAULT_KEYS)


def test_unknown_button_rejected():
    with pytest.raises(ValueError):
        InputState({'x': 'TURBO'})


def test_button_held_until_every_source_releases():
    state = InputState({})
    assert state.press('key:x', 'A')
    assert not state.press('mouse:A', 'A')  # already held: no change
    assert not state.release('key:x')
    assert state.buttons() == {'A'}
    assert state.release('mouse:A')
    assert state.buttons() == set()


def test_stick_combines_and_clamps():
    state = InputState({})
    state.press('key:i', 'LS_UP')
    state.press('key:l', 'LS_RIGHT')
    assert state.stick() == (1.0, 1.0)
    state.press('key:k', 'LS_DOWN')
    assert state.stick() == (1.0, 0.0)
    assert state.buttons() == set()


def test_clear_reports_change():
    state = InputState({})
    assert not state.clear()
    state.press('key:x', 'A')
    assert state.clear() and state.buttons() == set()


def test_apply_to_controller_sets_exact_state():
    calls = []

    class Controller:
        def release(self, *b): calls.append(('release', b))
        def hold(self, *b): calls.append(('hold', tuple(sorted(b))))
        def set_stick(self, *a): calls.append(('stick', a))

    apply_to_controller(Controller(), {'A', 'B'}, (0.0, 1.0))
    assert calls == [('release', ()), ('hold', ('A', 'B')), ('stick', ('left', 0.0, 1.0))]
