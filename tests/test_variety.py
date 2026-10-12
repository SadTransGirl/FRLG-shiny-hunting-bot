"""The seed check: same starter screen vs a different one, through capture noise."""
import random

import cv2
import numpy as np

from shinybot.variety import EXPECTED_SAME, WARN_AFTER, SeedCheck

SPRITE_BOX = (150, 120, 300, 300)
NATURES = ['HARDY', 'LONELY', 'BRAVE', 'ADAMANT', 'NAUGHTY', 'BOLD', 'DOCILE', 'RELAXED', 'IMPISH',
           'LAX', 'TIMID', 'HASTY', 'SERIOUS', 'JOLLY', 'NAIVE', 'MODEST', 'MILD', 'QUIET', 'BASHFUL',
           'RASH', 'CALM', 'GENTLE', 'SASSY', 'CAREFUL', 'QUIRKY']


def summary_screen(nature, male=True, shiny=False, seed=0):
    """A stand-in for the INFO page as a capture card delivers it: noisy and JPEG-compressed."""
    frame = np.full((720, 1280, 3), (230, 236, 240), np.uint8)
    cv2.rectangle(frame, (140, 40), (1140, 100), (80, 160, 220), -1)                 # title bar
    cv2.putText(frame, 'POKEMON INFO', (160, 85), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (255, 255, 255), 3)
    cv2.circle(frame, (300, 270), 110, (60, 200, 255) if shiny else (60, 120, 200), -1)  # the sprite
    cv2.putText(frame, 'M' if male else 'F', (520, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.6,
                (200, 80, 40) if male else (60, 60, 220), 4)
    cv2.putText(frame, f'{nature} nature.', (160, 560), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (60, 60, 60), 4)
    cv2.putText(frame, 'Met in PALLET TOWN at Lv. 5.', (160, 630), cv2.FONT_HERSHEY_SIMPLEX, 1.5,
                (60, 60, 60), 4)
    rng = np.random.default_rng(seed)
    noisy = np.clip(frame + rng.normal(0, 6, frame.shape), 0, 255).astype(np.uint8)
    ok, jpeg = cv2.imencode('.jpg', noisy, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return cv2.imdecode(jpeg, cv2.IMREAD_COLOR)


def add(check, nature, male=True, shiny=False, seed=0):
    return check.add(summary_screen(nature, male, shiny, seed), summary_screen(nature, male, shiny, seed + 1))


def test_same_starter_matches_through_noise_and_different_ones_dont():
    check = SeedCheck(SPRITE_BOX)
    assert add(check, 'BOLD', seed=10).same is None          # nothing to compare with yet
    assert add(check, 'BOLD', seed=20).same                   # same starter, different noise
    assert add(check, 'BOLD', shiny=True, seed=30).same       # the sprite box is ignored
    assert add(check, 'CALM', seed=40).same is False          # another nature
    assert add(check, 'CALM', male=False, seed=50).same is False  # same nature, other gender


def test_stuck_seeds_are_reported():
    check = SeedCheck(SPRITE_BOX)
    for i in range(WARN_AFTER + 1):
        add(check, 'BOLD', seed=2 * i)
    assert check.same_share == 1.0
    assert 'same few seeds' in check.verdict()


def test_random_starters_pass():
    check = SeedCheck(SPRITE_BOX)
    rng = random.Random(1)
    for i in range(60):
        add(check, rng.choice(NATURES), male=rng.random() < 7 / 8, seed=2 * i)
    assert check.verdict() is None
    assert check.same_share < 0.15
    assert 'resets showed a different starter' in check.summary()
    assert 0.02 < EXPECTED_SAME < 0.04


def test_csv_log(tmp_path):
    check = SeedCheck(SPRITE_BOX, tmp_path / 'resets.csv')
    result = add(check, 'BOLD')
    check.record(1, [1.25, 3.5], 'normal', result)
    check.record(2, [0.5, 4.0], 'normal', add(check, 'BOLD', seed=5))
    lines = (tmp_path / 'resets.csv').read_text().splitlines()
    assert lines[0].startswith('reset,time,random_waits_s')
    assert lines[1].split(',')[2] == '1.250 3.500' and lines[2].split(',')[4] == '1'
