"""The phone registers once, reconnects to its assigned ports, and receives stream controls."""
from fastapi.testclient import TestClient

from app.main import app
from app import peers, peer_presence


def register(client, **changes):
    return client.post('/api/peers/register', json={
        'name': 'fairphone', 'tailscale_ip': '100.98.253.67',
        'capture_mode': 'microphone', **changes,
    })


def test_registration_is_idempotent_and_does_not_restart_existing_peers(session):
    client = TestClient(app)
    first = register(client)
    assert first.status_code == 200
    config = first.json()
    assert config['ports']['playback_source'] == 10044
    assert config['fec'] == 'disable'
    assert config['sample_rate'] == 44100
    assert config['send_enabled'] is True
    assert config['receive_enabled'] is False
    modified = peers.MANAGED_CONF_FILE.stat().st_mtime_ns
    assert register(client).json() == config
    assert peers.MANAGED_CONF_FILE.stat().st_mtime_ns == modified
    assert len([p for p in peers.load_peers() if p.name == 'fairphone']) == 1


def test_registration_rejects_config_injection_and_non_tailnet_addresses(session):
    client = TestClient(app)
    for values in [
        {'name': 'bad"name'}, {'tailscale_ip': '127.0.0.1'},
        {'tailscale_ip': '100.1.2.3'}, {'capture_mode': 'system-mic-injection'},
    ]:
        assert register(client, **values).status_code == 422
    assert [p.name for p in peers.load_peers()] == ['sagedeck', 'sage-dev', 'sage']


def test_android_cannot_take_over_a_desktop_peer(session):
    client = TestClient(app)
    assert register(client, name='sagedeck').status_code == 409


def test_control_delivery_status_and_presence_survive_reconnect(session):
    client = TestClient(app)
    register(client)
    with client.websocket_connect('/ws/peer/fairphone?client=android') as socket:
        initial = socket.receive_json()
        assert initial['type'] == 'peer_config'
        assert initial['name'] == 'fairphone'
        assert 'fairphone' in peer_presence.connected_peers
        result = client.post('/api/peers/fairphone/streams', json={'send_enabled': False, 'receive_enabled': True})
        assert result.status_code == 200
        control = socket.receive_json()
        assert control['type'] == 'peer_streams'
        assert control['send_enabled'] is False
        assert control['receive_enabled'] is True
        socket.send_json({'type': 'peer_status', 'send_active': False, 'receive_active': True, 'error': None})
        # Acknowledgement proves the server processed the status message.
        assert socket.receive_json()['type'] == 'peer_status'
    assert 'fairphone' not in peer_presence.connected_peers
    with client.websocket_connect('/ws/peer/fairphone?client=android') as socket:
        assert socket.receive_json()['send_enabled'] is False
    assert peers.load_peers()[-1].receive_enabled is True


def test_a_second_presence_socket_does_not_make_the_first_go_offline(session):
    client = TestClient(app)
    with client.websocket_connect('/ws/peer/sagedeck'):
        with client.websocket_connect('/ws/peer/sagedeck'):
            assert 'sagedeck' in peer_presence.connected_peers
        assert 'sagedeck' in peer_presence.connected_peers
    assert 'sagedeck' not in peer_presence.connected_peers


def test_removing_phone_closes_control_socket_and_stops_its_session(session):
    client = TestClient(app)
    register(client)
    with client.websocket_connect('/ws/peer/fairphone?client=android') as socket:
        socket.receive_json()
        assert client.post('/peers/fairphone/delete').status_code == 200
        assert socket.receive_json()['type'] == 'peer_removed'
        from starlette.websockets import WebSocketDisconnect
        import pytest
        with pytest.raises(WebSocketDisconnect) as stopped:
            socket.receive_json()
        assert stopped.value.code == 1008
    assert 'fairphone' not in peer_presence.connected_peers


def test_phone_cards_offer_stream_controls_but_desktop_cards_do_not(session):
    client = TestClient(app)
    register(client, capture_mode='device_audio')
    html = client.get('/peers').text
    assert 'Send device audio' in html
    assert 'Listen to headset audio' in html
    assert html.count('class="client-controls"') == 1


def test_android_microphone_is_a_source_and_receives_headset_playback(session):
    client = TestClient(app)
    register(client)
    text = peers.MANAGED_CONF_FILE.read_text()
    assert 'media.class = "Audio/Source"' in text
    assert 'stream.capture.sink = true' in text
    assert 'target.object = "alsa_output.usb-' in text
    register(client, capture_mode='device_audio')
    text = peers.MANAGED_CONF_FILE.read_text()
    assert 'media.class = "Audio/Source"' not in text
    assert 'stream.capture.sink = true' in text


def test_listen_only_phone_receives_without_a_send_control(session):
    client = TestClient(app)
    config = register(client, capture_mode='none').json()
    assert config['capture_mode'] == 'none'
    assert config['send_enabled'] is False
    assert config['receive_enabled'] is True
    html = client.get('/peers').text
    assert 'data-stream="send"' not in html
    assert 'Listen to headset audio' in html
    result = client.post('/api/peers/fairphone/streams', json={'send_enabled': True, 'receive_enabled': True})
    assert result.json()['send_enabled'] is False
    config = register(client).json()
    assert config['send_enabled'] is True
    assert config['receive_enabled'] is False


def test_only_a_local_dev_server_accepts_a_loopback_phone(session, monkeypatch):
    client = TestClient(app)
    monkeypatch.delenv('BRAGI_LOCAL_DEV', raising=False)
    assert register(client, tailscale_ip='127.0.0.1').status_code == 422
    monkeypatch.setenv('BRAGI_LOCAL_DEV', '1')
    assert register(client, tailscale_ip='127.0.0.1').status_code == 200
    assert register(client, tailscale_ip='10.0.2.15').status_code == 422


def test_phone_card_shows_the_delays_the_phone_measures(session):
    client = TestClient(app)
    register(client)
    with client.websocket_connect('/ws/peer/fairphone?client=android') as socket:
        socket.receive_json()
        socket.send_json({'type': 'peer_status', 'send_active': True, 'receive_active': True,
                          'error': None, 'send_delay_ms': 31, 'listen_delay_ms': 102})
        status = socket.receive_json()
        assert status['send_delay_ms'] == 31
        assert status['listen_delay_ms'] == 102
        html = client.get('/peers').text
        assert 'Sending 31 ms · Listening 102 ms' in html
        # Anything that is not a plausible millisecond count is dropped, not shown.
        socket.send_json({'type': 'peer_status', 'send_active': True, 'receive_active': True,
                          'error': None, 'send_delay_ms': '<b>', 'listen_delay_ms': 99999})
        status = socket.receive_json()
        assert status['send_delay_ms'] is None
        assert status['listen_delay_ms'] is None
        assert 'Sending · Listening' in client.get('/peers').text


def test_phone_card_warns_when_the_phone_is_not_at_the_registered_address(session):
    # Issue #2: the phone registered its carrier address, and listening was silent.
    # Tailscale Serve passes the phone's real tailnet address in X-Forwarded-For.
    client = TestClient(app)
    register(client, tailscale_ip='100.83.223.246')
    with client.websocket_connect('/ws/peer/fairphone?client=android',
                                  headers={'X-Forwarded-For': '100.98.253.67'}) as socket:
        socket.receive_json()
        socket.send_json({'type': 'peer_status', 'send_active': True, 'receive_active': True, 'error': None})
        warning = socket.receive_json()['address_warning']
        assert warning == 'Phone is at 100.98.253.67, Bragi sends to 100.83.223.246. Reopen the app.'
        assert warning in client.get('/peers').text


def test_no_address_warning_when_the_address_matches_or_is_unknown(session):
    client = TestClient(app)
    register(client)
    for headers in [{'X-Forwarded-For': '100.98.253.67'}, {}, {'X-Forwarded-For': '172.17.0.1'}]:
        with client.websocket_connect('/ws/peer/fairphone?client=android', headers=headers) as socket:
            socket.receive_json()
            socket.send_json({'type': 'peer_status', 'send_active': True, 'receive_active': True, 'error': None})
            assert socket.receive_json()['address_warning'] is None
