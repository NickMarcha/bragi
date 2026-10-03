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
    assert 'Listen to headset mic' in html
    assert html.count('class="client-controls"') == 1
