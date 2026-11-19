"""Where this hub is, for the tray apps, and moving them (app/hub_info.py, bragi#61)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import hub_info, peers
from app.main import app


@pytest.mark.parametrize("typed,url", [
    ("sagehub.tail08dfa.ts.net", "wss://sagehub.tail08dfa.ts.net/ws/peer"),
    ("https://sagehub.tail08dfa.ts.net/", "wss://sagehub.tail08dfa.ts.net/ws/peer"),
    ("wss://sagehub.tail08dfa.ts.net/ws/peer", "wss://sagehub.tail08dfa.ts.net/ws/peer"),
    ("  newhub:8443 ", "wss://newhub:8443/ws/peer"),
])
def test_what_people_type_becomes_a_peer_socket_url(typed, url):
    assert hub_info.ws_url(typed) == url


@pytest.mark.parametrize("typed", ["", "https://", "bad host", "host:99999", "-x.example"])
def test_bad_addresses_are_refused(typed):
    with pytest.raises(ValueError):
        hub_info.ws_url(typed)


def test_the_hub_message(monkeypatch):
    monkeypatch.setattr(hub_info, "_tailscale", lambda: {"name": "sagehub.tail08dfa.ts.net", "ip": "100.84.140.96"})
    monkeypatch.setattr(hub_info, "lan_ip", lambda: "192.168.1.32")
    monkeypatch.setattr(hub_info, "_kvm_installed", lambda: True)
    hub_info.forget()
    assert hub_info.message() == {
        "type": "hub", "ws_url": "wss://sagehub.tail08dfa.ts.net/ws/peer", "tailscale_ip": "100.84.140.96",
        "lan_ip": "192.168.1.32", "deskflow_hosts": ["192.168.1.32", "100.84.140.96"]}
    # Without Tailscale and without the KVM.
    monkeypatch.setattr(hub_info, "_tailscale", lambda: {})
    monkeypatch.setattr(hub_info, "_kvm_installed", lambda: False)
    hub_info.forget()
    msg = hub_info.message()
    assert msg["ws_url"] is None and msg["tailscale_ip"] is None and msg["deskflow_hosts"] == []


def test_a_tray_app_hears_where_the_hub_is_after_its_hello(session):
    client = TestClient(app)
    with client.websocket_connect("/ws/peer/deck") as deck:
        assert deck.receive_json()["type"] == "kvm"
        deck.send_json({"type": "client_hello", "features": []})
        hub = deck.receive_json()
        assert hub["type"] == "hub" and hub["lan_ip"] == "192.168.1.32"


def test_moving_tells_tray_apps_and_phones(session):
    """Phones follow too since bragi#67: the app saves the new address and registers there."""
    peers.register_android_peer("fairphone", "100.64.0.20", "microphone")
    client = TestClient(app)
    with client.websocket_connect("/ws/peer/deck") as deck,             client.websocket_connect("/ws/peer/fairphone?client=android") as phone:
        deck.receive_json()  # kvm
        assert phone.receive_json()["type"] == "peer_config"
        page = client.post("/hub/move", data={"url": "https://newhub.example.ts.net"}).text
        assert "Told 2 apps to switch to wss://newhub.example.ts.net/ws/peer" in page
        moved = {"type": "hub_moved", "ws_url": "wss://newhub.example.ts.net/ws/peer"}
        assert deck.receive_json() == moved
        assert phone.receive_json() == moved
    assert "Type the new hub" in client.post("/hub/move", data={"url": "not a host"}).text


def test_the_settings_section(session):
    page = TestClient(app).get("/hub").text
    assert "wss://hub.example.ts.net/ws/peer" in page and "192.168.1.32" in page
    assert "Move tray apps and phones to another hub" in page
