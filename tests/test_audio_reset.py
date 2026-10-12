"""The dashboard's "Reset audio" button only writes a request file. The host's
bragi-audio-reset.path picks it up, snapshots the graph into incidents/ and
restarts PipeWire (host/systemd/)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import audio_reset
from app.main import app

NOW = datetime(2026, 10, 12, 10, 30, tzinfo=timezone.utc)


def incident(data_dir, name: str, done: bool = True):
    path = data_dir / 'incidents' / name
    path.mkdir(parents=True)
    if done:
        (path / 'done').write_text('')
    return path


def test_button_writes_a_request_the_host_path_unit_can_watch(data_dir, monkeypatch):
    monkeypatch.setattr(audio_reset, '_now', lambda: NOW)
    response = TestClient(app).post('/audio/reset')
    assert response.status_code == 200
    assert (data_dir / 'reset-request').read_text() == '2026-10-12T10:30:00+00:00\n'
    assert 'Resetting' in response.text


def test_second_press_while_pending_does_not_restart_twice(data_dir, monkeypatch):
    monkeypatch.setattr(audio_reset, '_now', lambda: NOW)
    client = TestClient(app)
    client.post('/audio/reset')
    monkeypatch.setattr(audio_reset, '_now', lambda: NOW + timedelta(seconds=20))
    assert client.post('/audio/reset').status_code == 409
    assert (data_dir / 'reset-request').read_text() == '2026-10-12T10:30:00+00:00\n'


def test_finished_snapshot_clears_pending_and_shows_last_reset(data_dir, monkeypatch):
    monkeypatch.setattr(audio_reset, '_now', lambda: NOW)
    audio_reset.request()
    incident(data_dir, '20260912-050000-nightly')
    incident(data_dir, '20261012-103004-manual')
    view = audio_reset.view()
    assert view['state'] == 'idle'
    assert view['last'] == {'when': '2026-10-12 10:30', 'reason': 'manual'}


def test_unfinished_snapshot_is_still_pending(data_dir, monkeypatch):
    monkeypatch.setattr(audio_reset, '_now', lambda: NOW)
    audio_reset.request()
    incident(data_dir, '20261012-103004-manual', done=False)
    assert audio_reset.view()['state'] == 'pending'


def test_request_the_host_never_picked_up_is_reported_and_can_be_retried(data_dir, monkeypatch):
    monkeypatch.setattr(audio_reset, '_now', lambda: NOW)
    audio_reset.request()
    monkeypatch.setattr(audio_reset, '_now', lambda: NOW + timedelta(minutes=3))
    view = audio_reset.view()
    assert view['state'] == 'stale'
    response = TestClient(app).post('/audio/reset')
    assert response.status_code == 200


def test_dashboard_shows_the_button(session):
    page = TestClient(app).get('/').text
    assert 'hx-post="/audio/reset"' in page
