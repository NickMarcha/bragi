"""The Bragi Client's "Connect to Bragi" window registers through
/api/peers/register-desktop and writes the ports it gets back into the
link unit's env file. Before this, those six ports were copied by hand from
peers.yaml."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import auth
from app import peers as peers_module
from app.main import app


def register(name="laptop", ip="100.64.0.30", client=None):
    return (client or TestClient(app)).post("/api/peers/register-desktop", json={"name": name, "tailscale_ip": ip})


def peer(name):
    return next(p for p in peers_module.load_peers() if p.name == name)


def test_a_new_desktop_gets_a_managed_roc_peer_and_its_ports(session):
    response = register()

    assert response.status_code == 200
    assert response.json() == {"name": "laptop", "ports": {
        "mic_source": 10041, "mic_repair": 10042, "mic_control": 10043,
        "playback_source": 10044, "playback_repair": 10045, "playback_control": 10046}}
    laptop = peer("laptop")
    assert (laptop.protocol, laptop.managed, laptop.client_kind) == ("roc", True, "desktop")
    assert "remote.ip = 100.64.0.30" in peers_module.MANAGED_CONF_FILE.read_text()


def test_connecting_again_keeps_the_ports_and_follows_a_new_address(session):
    first = register().json()

    again = register(ip="100.64.0.31")

    assert again.json() == first
    assert peer("laptop").tailscale_ip == "100.64.0.31"


def test_a_roc_peer_added_from_the_dashboard_is_claimed(session):
    added = peers_module.add_roc_peer("laptop", "100.64.0.30")

    response = register()

    assert response.status_code == 200
    assert response.json()["ports"]["mic_source"] == added.ports.mic_source
    assert peer("laptop").client_kind == "desktop"


def test_a_hand_configured_peer_is_returned_untouched(session):
    response = register("deck", "100.64.0.99")

    assert response.status_code == 200
    assert response.json()["ports"]["mic_source"] == 10001
    deck = peer("deck")
    assert (deck.managed, deck.tailscale_ip, deck.client_kind) == (False, "100.64.0.11", None)


def test_names_owned_by_a_phone_or_a_vban_peer_are_refused(session):
    peers_module.register_android_peer("fairphone", "100.64.0.20", "microphone")

    assert register("fairphone").status_code == 409
    assert register("desktop").status_code == 409


def test_only_tailnet_addresses_are_accepted(session):
    assert register(ip="192.168.1.20").status_code == 422


def test_it_stays_reachable_once_the_dashboard_has_a_password(session):
    auth.set_password("hunter22")

    assert register().status_code == 200


def register_vban(name="desktop", ip="100.64.0.40", client=None):
    return (client or TestClient(app)).post("/api/peers/register-desktop",
                                            json={"name": name, "tailscale_ip": ip, "protocol": "vban"})


def test_the_windows_client_claims_a_vban_peer_and_keeps_its_streams(session):
    response = register_vban()

    assert response.status_code == 200
    assert response.json() == {"name": "desktop", "vban": {
        "port": 6980, "stream_send": "BragiMic", "stream_receive": "DesktopAudio"}}
    desktop = peer("desktop")
    assert (desktop.protocol, desktop.client_kind, desktop.tailscale_ip) == ("vban", "desktop", "100.64.0.40")
    assert 'vban.ip = "100.64.0.40"' in peers_module.MANAGED_CONF_FILE.read_text()


def test_a_new_windows_client_gets_a_vban_peer_with_the_dashboard_defaults(session):
    response = register_vban("sage", "100.64.0.41")

    assert response.status_code == 200
    assert response.json()["vban"] == {"port": 6980, "stream_send": "BragiMic", "stream_receive": "SageAudio"}
    assert peer("sage").protocol == "vban"


def test_a_windows_client_cannot_take_a_roc_or_phone_name(session):
    peers_module.register_android_peer("fairphone", "100.64.0.20", "microphone")
    register("laptop")

    assert register_vban("fairphone").status_code == 409
    assert register_vban("laptop").status_code == 409
