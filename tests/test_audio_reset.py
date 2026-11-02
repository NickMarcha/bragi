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


# The nightly reset's hour lives in data/nightly-reset, set from the
# dashboard's Settings dialog. The host timer fires hourly and
# bragi-audio-reset.sh only resets in that hour.

def test_the_nightly_reset_defaults_to_five_in_the_morning(data_dir):
    assert audio_reset.nightly_hour() == 5


def test_the_nightly_hour_is_written_for_the_host_script(data_dir):
    audio_reset.set_nightly_hour(3)
    assert (data_dir / 'nightly-reset').read_text() == '3\n'
    assert audio_reset.nightly_hour() == 3

    audio_reset.set_nightly_hour(None)
    assert (data_dir / 'nightly-reset').read_text() == 'off\n'
    assert audio_reset.nightly_hour() is None


def test_a_bad_nightly_hour_is_refused(data_dir):
    for bad in (24, -1, True, '3', 2.5):
        try:
            audio_reset.set_nightly_hour(bad)
        except ValueError:
            continue
        raise AssertionError(f'{bad!r} was accepted')
    assert not (data_dir / 'nightly-reset').exists()


async def test_the_settings_dialog_changes_the_hour_for_every_tab(data_dir, session, broadcasts):
    from app import views, ws

    await ws.apply_action({'action': 'set_nightly_reset', 'value': 22})

    assert broadcasts.of_type('nightly_reset') == [{'type': 'nightly_reset', 'hour': 22}]
    assert views.build_state()['nightly_reset'] == {'hour': 22}


async def test_a_bad_hour_from_a_client_changes_nothing(data_dir, session, broadcasts):
    from app import ws

    await ws.apply_action({'action': 'set_nightly_reset', 'value': 99})

    assert broadcasts.of_type('nightly_reset') == []
    assert audio_reset.nightly_hour() == 5


def test_the_settings_dialog_shows_the_saved_hour(data_dir, session):
    audio_reset.set_nightly_hour(2)
    html = TestClient(app).get('/').text
    assert '<option value="2" selected>02:00</option>' in html
