"""The peer socket on the LAN (app/lan_peer.py, bragi#90): only
/ws/peer/<name> with that name's token, and the "hub" message tells each
tray app its token and the certificate to pin."""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import lan_peer
from app.main import app


@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setenv("BRAGI_LAN_PEER_PORT", "8443")
    monkeypatch.setattr(lan_peer, "fingerprint", lambda: "ab" * 32)


def test_tokens_are_per_computer_and_stay_the_same():
    assert lan_peer.token("sage") == lan_peer.token("sage")
    assert lan_peer.token("sage") != lan_peer.token("sage-dev")
    assert len(lan_peer.token("sage")) == 64


def test_without_a_port_nothing_is_offered(monkeypatch):
    monkeypatch.delenv("BRAGI_LAN_PEER_PORT", raising=False)
    assert lan_peer.info("sage") is None


def test_only_the_peer_socket_with_its_token_gets_through(lan, session):
    client = TestClient(lan_peer.LanPeerApp(app))
    with client.websocket_connect("/ws/peer/sage", headers={"x-bragi-lan-token": lan_peer.token("sage")}) as tray:
        assert tray.receive_json()["type"] == "kvm"
    # Another computer's token, no token, or any other path: refused.
    for path, headers in [("/ws/peer/sage", {"x-bragi-lan-token": lan_peer.token("sage-dev")}),
                          ("/ws/peer/sage", {}),
                          ("/ws", {"x-bragi-lan-token": lan_peer.token("sage")})]:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers=headers) as ws:
                ws.receive_json()
    assert client.get("/").status_code == 404
    assert client.get("/api/v1/state").status_code == 404


def test_the_hub_message_carries_the_token_and_the_certificate(lan, session):
    with TestClient(app).websocket_connect("/ws/peer/sage") as tray:
        tray.receive_json()  # kvm
        tray.send_json({"type": "client_hello", "features": []})
        hub = tray.receive_json()
    assert hub["lan_peer"] == {"port": 8443, "cert_sha256": "ab" * 32, "token": lan_peer.token("sage")}


@pytest.mark.skipif(not shutil.which("openssl"), reason="makes a certificate with openssl")
def test_the_certificate_is_made_once():
    assert lan_peer.ensure_certificate()
    first = lan_peer.fingerprint()
    assert first and len(first) == 64
    assert lan_peer.ensure_certificate() and lan_peer.fingerprint() == first
