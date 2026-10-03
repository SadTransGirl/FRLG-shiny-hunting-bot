from shinybot.recorder import Recorder, chord_name, merge_repeats
from shinybot.sequence import Step


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def play(recorder, clock, events):
    """events: (seconds_from_start, held_buttons)"""
    start = clock.now
    for at, held in events:
        clock.now = start + at
        recorder.update(set(held))


def test_presses_become_steps_with_hold_and_wait():
    clock = Clock()
    rec = Recorder(clock)
    play(rec, clock, [(0.0, {'A'}), (0.1, set()), (2.1, {'DOWN'}), (2.3, set())])
    clock.now += 1.5
    assert rec.stop() == [Step('A', hold=0.1, wait=2.0), Step('DOWN', hold=0.2, wait=1.5)]


def test_chord_collects_buttons_pressed_together():
    clock = Clock()
    rec = Recorder(clock)
    play(rec, clock, [(0.0, {'A'}), (0.05, {'A', 'B'}), (0.1, {'A', 'B', 'PLUS', 'MINUS'}),
                      (0.4, {'MINUS'}), (0.5, set())])
    clock.now += 4.0
    assert rec.stop() == [Step('A+B+PLUS+MINUS', hold=0.5, wait=4.0)]


def test_stop_while_holding_finishes_the_press():
    clock = Clock()
    rec = Recorder(clock)
    play(rec, clock, [(0.0, {'LEFT'})])
    clock.now += 1.0
    assert rec.stop() == [Step('LEFT', hold=1.0, wait=0.0)]


def test_chord_name_order_is_stable():
    assert chord_name({'MINUS', 'PLUS', 'B', 'A'}) == 'A+B+PLUS+MINUS'


def test_merge_repeats_combines_mashing_but_keeps_long_gaps():
    steps = [Step('A', 0.1, 1.4)] + [Step('B', 0.1, 0.5), Step('B', 0.15, 0.7), Step('B', 0.1, 0.6)] \
        + [Step('B', 0.1, 3.0), Step('PLUS', 0.1, 1.0, note='menu')]
    merged = merge_repeats(steps)
    assert merged == [Step('A', 0.1, 1.4), Step('B', 0.15, 0.7, repeat=3),
                      Step('B', 0.1, 3.0), Step('PLUS', 0.1, 1.0, note='menu')]
    assert steps[1].repeat == 1  # input not modified
