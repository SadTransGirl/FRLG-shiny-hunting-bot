import asyncio
import json

import cv2
import numpy as np
import pytest

from shinybot.config import Config
from shinybot.detect import ShinyDetector
from shinybot.hunt import SETUP_FRAME, Hunter, HuntStopped
from shinybot.sequence import STARTER_SEQUENCE, Step, run_steps, steps_from_config

SPRITE_BOX = (100, 60, 80, 80)
SCREEN_BOX = (0, 0, 320, 30)
NORMAL = (40, 140, 240)  # BGR orange, like Charmander
SHINY = (30, 200, 250)  # BGR yellow-gold, like shiny Charmander

rng = np.random.default_rng(0)


def summary_frame(body=NORMAL, title=(200, 120, 60)) -> np.ndarray:
    """A fake summary screen: title bar, background, and a blob 'sprite'."""
    frame = np.full((240, 320, 3), 235, np.uint8)
    frame[0:30] = title
    cv2.putText(frame, 'POKEMON INFO', (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    cv2.circle(frame, (140, 100), 28, body, -1)
    cv2.circle(frame, (130, 92), 5, (20, 20, 20), -1)  # eye
    noise = rng.normal(0, 3, frame.shape)  # capture card noise
    return np.clip(frame + noise, 0, 255).astype(np.uint8)


def calibrated_detector() -> ShinyDetector:
    detector = ShinyDetector(SPRITE_BOX, SCREEN_BOX)
    for _ in range(3):
        detector.add_calibration_frame(summary_frame())
    assert detector.finish_calibration() == []
    return detector


def test_normal_frames_pass():
    detector = calibrated_detector()
    for _ in range(20):
        assert detector.check(summary_frame()).verdict == 'normal'


def test_shiny_palette_is_detected():
    assert calibrated_detector().check(summary_frame(body=SHINY)).verdict == 'shiny'


def test_wrong_screen_is_detected():
    overworld = np.clip(rng.normal(90, 40, (240, 320, 3)), 0, 255).astype(np.uint8)
    assert calibrated_detector().check(overworld).verdict == 'wrong_screen'


def test_calibration_warns_when_samples_disagree():
    detector = ShinyDetector(SPRITE_BOX, SCREEN_BOX)
    for body in (NORMAL, NORMAL, SHINY):
        detector.add_calibration_frame(summary_frame(body=body))
    assert detector.finish_calibration()


def test_sequence_roundtrip_and_validation():
    data = json.loads(json.dumps([s.__dict__ for s in STARTER_SEQUENCE]))
    assert steps_from_config(data) == STARTER_SEQUENCE
    with pytest.raises(ValueError):
        steps_from_config([{'press': 'TURBO'}])


class FakeController:
    connected = True

    def __init__(self):
        self.presses = []

    async def press(self, *buttons, duration=0.1, wait=0.05):
        self.presses.append(buttons)

    async def reconnect(self):
        self.connected = True


class FakeFrames:
    def __init__(self, frames):
        self.frames = list(frames)

    def latest(self):
        return self.frames.pop(0)


def make_hunter(tmp_path, frames, **config_changes):
    config = Config(sprite_box=SPRITE_BOX, screen_box=SCREEN_BOX,
                    sequences={'test': [Step('A+B+PLUS+MINUS', wait=0), Step('A', wait=0, repeat=2)]},
                    active_sequence='test', **config_changes)
    return Hunter(FakeController(), FakeFrames(frames), config, tmp_path)


async def test_run_steps_presses_buttons_in_order():
    controller = FakeController()
    await run_steps(controller, [Step('A+B', wait=0), Step('DOWN', wait=0, repeat=2)])
    assert controller.presses == [('A', 'B'), ('DOWN',), ('DOWN',)]


async def test_hunt_stops_on_shiny(tmp_path):
    frames = [summary_frame() for _ in range(3 + 5)] + [summary_frame(body=SHINY)]
    hunter = make_hunter(tmp_path, frames)
    _frame, result = await hunter.run()
    assert result.verdict == 'shiny'
    assert hunter.stats.resets == 9
    assert (tmp_path / 'SHINY_reset_9.png').exists()
    assert json.loads((tmp_path / 'stats.json').read_text())['resets'] == 9


async def test_hunt_stops_after_repeated_wrong_screens(tmp_path):
    wrong = np.zeros((240, 320, 3), np.uint8)
    hunter = make_hunter(tmp_path, [summary_frame() for _ in range(3)] + [wrong] * 3)
    with pytest.raises(HuntStopped, match='missed the summary screen'):
        await hunter.run()


async def test_calibration_checks_against_setup_screen(tmp_path):
    cv2.imwrite(str(tmp_path / SETUP_FRAME), summary_frame())
    wrong = np.zeros((240, 320, 3), np.uint8)
    hunter = make_hunter(tmp_path, [wrong] * 3)
    with pytest.raises(HuntStopped, match='did not end on the screen chosen in setup'):
        await hunter.run()


async def test_hunt_reconnects_before_a_reset(tmp_path):
    hunter = make_hunter(tmp_path, [summary_frame() for _ in range(4)] + [summary_frame(body=SHINY)])
    hunter.controller.connected = False
    await hunter.run()
    assert hunter.controller.connected


def test_config_roundtrip(tmp_path):
    path = tmp_path / 'shinybot.json'
    config = Config(camera=2, sprite_box=SPRITE_BOX, screen_box=SCREEN_BOX)
    config.save(path)
    assert Config.load(path) == config


def test_subtle_palette_shift_is_detected_despite_noisy_capture():
    """A small colour change under heavy compression-like noise still trips the colour-shift check."""
    def noisy(body):
        frame = summary_frame(body=body).astype(np.float64)
        return np.clip(frame + rng.normal(0, 12, frame.shape), 0, 255).astype(np.uint8)

    detector = ShinyDetector(SPRITE_BOX, SCREEN_BOX)
    for _ in range(3):
        detector.add_calibration_frame(noisy(NORMAL))
    detector.finish_calibration()
    subtle = (NORMAL[0] + 22, NORMAL[1] + 18, NORMAL[2] - 20)
    assert all(detector.check(noisy(NORMAL)).verdict == 'normal' for _ in range(20))
    assert detector.check(noisy(subtle)).verdict == 'shiny'


def test_old_single_sequence_config_is_migrated(tmp_path):
    path = tmp_path / 'shinybot.json'
    path.write_text(json.dumps({'camera': 1, 'sequence': [{'press': 'A', 'wait': 9.0}]}))
    config = Config.load(path)
    assert config.camera == 1
    assert config.active_sequence == 'Starter'
    assert config.sequence == [Step('A', wait=9.0)]
    config.save(path)
    assert 'sequence' not in json.loads(path.read_text())


def test_default_sequences_are_independent_copies():
    a, b = Config(), Config()
    a.sequence[0].wait = 99
    assert b.sequence[0].wait != 99
