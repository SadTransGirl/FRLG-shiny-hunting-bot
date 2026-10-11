"""The session record that explains runs which vanished without an error."""
import json
import logging
import time

from shinybot import app


def make(tmp_path, interval=3600.0):
    return app.SessionMonitor(tmp_path / 'session.json', interval=interval)


def test_a_clean_exit_is_not_reported(tmp_path, caplog):
    session = make(tmp_path)
    session.set_state('hunt, 12 resets')
    session.end('window closed')
    record = json.loads((tmp_path / 'session.json').read_text())
    assert record['closed'] and record['reason'] == 'window closed' and record['state'] == 'hunt, 12 resets'
    with caplog.at_level(logging.ERROR):
        assert make(tmp_path).previous is None
    assert not caplog.records


def test_a_vanished_run_is_reported(tmp_path, caplog):
    started = time.time() - 3 * 3600
    (tmp_path / 'session.json').write_text(json.dumps({
        'pid': 1, 'started': started, 'last_beat': started + 2 * 3600 + 5 * 60, 'state': 'hunt, 4321 resets',
        'memory_mb': 512.0, 'peak_mb': 530.0, 'closed': False}))
    with caplog.at_level(logging.ERROR):
        session = make(tmp_path)
    assert 'ended without closing normally' in session.previous
    assert '2h05m in' in session.previous and 'hunt, 4321 resets' in session.previous
    assert 'memory 512 MB (peak 530)' in session.previous
    assert any('ended without closing' in r.getMessage() for r in caplog.records)
    # The new run's record is open until it ends.
    assert not json.loads((tmp_path / 'session.json').read_text())['closed']
    session.end('test')


def test_heartbeat_gap_means_sleep(tmp_path, caplog, monkeypatch):
    session = make(tmp_path, interval=0.05)
    real = time.time
    jump = {'by': 0.0}
    monkeypatch.setattr(app.time, 'time', lambda: real() + jump['by'])
    with caplog.at_level(logging.WARNING):
        jump['by'] = 600.0  # the clock moves 10 minutes between two beats: the PC slept
        deadline = real() + 2
        while real() < deadline and not any('asleep' in r.getMessage() for r in caplog.records):
            time.sleep(0.02)
    session.end('test')
    assert any('probably asleep' in r.getMessage() for r in caplog.records)


def test_memory_reading_works_here():
    memory = app.memory_mb()
    assert memory is None or memory > 1
