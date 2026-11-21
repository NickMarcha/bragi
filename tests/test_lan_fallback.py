"""Audio over the home network while the hub's Tailscale is down (app/lan_fallback.py, bragi#81)."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from app import lan_fallback, peers
from app.main import app

FIB_UP = """\
Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 100.64.0.0/10 2 0 2
        |-- 100.84.140.96
           /32 host LOCAL
     +-- 192.168.1.0/24 2 0 2
        |-- 192.168.1.0
           /24 link UNICAST
        |-- 192.168.1.32
           /32 host LOCAL
Local:
  +-- 0.0.0.0/0 3 0 5
        |-- 192.168.1.32
           /32 host LOCAL
"""

FIB_DOWN = """\
Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 192.168.1.0/24 2 0 2
        |-- 192.168.1.32
           /32 host LOCAL
        |-- 192.168.1.255
           /32 link BROADCAST
"""


@pytest.fixture(autouse=True)
def tailscale_mode(monkeypatch):
    monkeypatch.setattr(peers, "_use_lan", False)


def conf() -> str:
    return peers.MANAGED_CONF_FILE.read_text()


def test_the_hubs_own_addresses_come_from_fib_trie():
    assert lan_fallback.local_ipv4(FIB_UP) == {"100.84.140.96", "192.168.1.32"}
    assert lan_fallback.local_ipv4(FIB_DOWN) == {"192.168.1.32"}


def test_tailscale_is_up_only_with_a_tailnet_address(tmp_path):
    fib = tmp_path / "fib_trie"
    fib.write_text(FIB_UP)
    assert lan_fallback.tailscale_up(fib) is True
    fib.write_text(FIB_DOWN)
    assert lan_fallback.tailscale_up(fib) is False
    # Not Linux: can't tell, so nothing switches.
    assert lan_fallback.tailscale_up(tmp_path / "missing") is None


def test_the_switch_waits_three_checks_and_comes_back_at_once():
    switch = lan_fallback.Switch(down_checks=3)
    assert [switch.update(False) for _ in range(3)] == [False, False, True]
    assert switch.update(None) is True  # unknown keeps the last decision
    assert switch.update(False) is True
    assert switch.update(True) is False
    # A blip shorter than three checks never switches.
    assert [switch.update(u) for u in (False, False, True, False)] == [False, False, False, False]


def test_a_reported_lan_address_is_stored_with_the_peer(session):
    peers.register_desktop_peer("sage-dev", "100.64.0.30")
    assert peers.set_lan_ip("sage-dev", "192.168.1.20") is True
    assert peers.set_lan_ip("sage-dev", "192.168.1.20") is False  # unchanged: not saved again
    raw = yaml.safe_load(peers.PEERS_FILE.read_text())
    assert next(p for p in raw if p["name"] == "sage-dev")["lan_ip"] == "192.168.1.20"
    # Peers without one keep the old shape, so an older Bragi can still read the file.
    assert "lan_ip" not in next(p for p in raw if p["name"] == "deck")
    assert peers.set_lan_ip("sage-dev", None) is True
    assert next(p for p in peers.load_peers() if p.name == "sage-dev").lan_ip is None


@pytest.mark.parametrize("bad", ["100.64.0.30", "8.8.8.8", "127.0.0.1", "fe80::1", "not an ip"])
def test_only_private_ipv4_addresses_are_accepted(session, bad):
    peers.register_desktop_peer("sage-dev", "100.64.0.30")
    with pytest.raises(ValueError):
        peers.set_lan_ip("sage-dev", bad)


def test_phones_and_hand_configured_peers_keep_no_lan_address(session):
    peers.register_android_peer("fairphone", "100.64.0.20", "microphone")
    assert peers.set_lan_ip("fairphone", "192.168.1.40") is False
    assert peers.set_lan_ip("deck", "192.168.1.41") is False  # managed: false


def test_day_to_day_the_conf_uses_tailscale_addresses(session):
    peers.register_desktop_peer("sage-dev", "100.64.0.30")
    peers.set_lan_ip("sage-dev", "192.168.1.20")
    peers.set_lan_ip("desktop", "192.168.1.9")
    peers.sync_managed_conf()
    text = conf()
    assert "remote.ip = 100.64.0.30" in text and "192.168.1.20" not in text
    assert "destination.ip = 100.64.0.13" in text
    # The receive rule takes either address in either mode (vban_sender).
    assert 'vban.ip = "~^(100[.]64[.]0[.]13|192[.]168[.]1[.]9)$"' in text


def test_with_tailscale_down_the_conf_uses_lan_addresses(session):
    peers.register_desktop_peer("sage-dev", "100.64.0.30")
    peers.register_desktop_peer("laptop", "100.64.0.31")  # never reported a LAN address
    peers.register_android_peer("fairphone", "100.64.0.20", "microphone")
    peers.set_lan_ip("sage-dev", "192.168.1.20")
    peers.set_lan_ip("desktop", "192.168.1.9")

    assert peers.set_lan_mode(True) is True
    text = conf()
    assert "remote.ip = 192.168.1.20" in text
    # VBAN: the send target and the receive rule that matches the sender.
    assert "destination.ip = 192.168.1.9" in text and 'vban.ip = "~^(100[.]64[.]0[.]13|192[.]168[.]1[.]9)$"' in text
    assert "remote.ip = 100.64.0.31" in text  # no LAN address: as it was
    assert "remote.ip = 100.64.0.20" in text  # phones stay on Tailscale

    assert peers.set_lan_mode(False) is True
    back = conf()
    assert "remote.ip = 192.168.1." not in back and "destination.ip = 192.168.1." not in back


def test_a_new_lan_address_reaches_the_conf_only_in_lan_mode(session):
    peers.register_desktop_peer("sage-dev", "100.64.0.30")
    peers.set_lan_ip("sage-dev", "192.168.1.20")
    assert "192.168.1.20" not in conf()
    peers.set_lan_mode(True)
    peers.set_lan_ip("sage-dev", "192.168.1.21")
    assert "remote.ip = 192.168.1.21" in conf()


def test_switching_with_the_headset_off_waits(session, monkeypatch):
    peers.register_desktop_peer("sage-dev", "100.64.0.30")
    peers.set_lan_ip("sage-dev", "192.168.1.20")
    monkeypatch.setattr(peers, "_headset_mic_source_name", lambda: None)
    assert peers.set_lan_mode(True) is False
    assert not peers.using_lan() and "192.168.1.20" not in conf()


def test_switching_twice_writes_the_conf_once(session):
    """bragi-peers.path restarts the peers on every write."""
    peers.register_desktop_peer("sage-dev", "100.64.0.30")
    peers.set_lan_ip("sage-dev", "192.168.1.20")
    peers.set_lan_mode(True)
    written = peers.MANAGED_CONF_FILE.stat().st_mtime_ns
    peers.set_lan_mode(True)
    assert peers.MANAGED_CONF_FILE.stat().st_mtime_ns == written


def test_a_tray_app_reports_its_lan_address(session):
    peers.register_desktop_peer("sage-dev", "100.64.0.30")
    client = TestClient(app)
    with client.websocket_connect("/ws/peer/sage-dev") as tray:
        assert tray.receive_json()["type"] == "kvm"
        tray.send_json({"type": "peer_lan", "lan_ip": "192.168.1.20"})
        tray.send_json({"type": "peer_lan", "lan_ip": "100.64.0.30"})  # ignored
        tray.send_json({"type": "client_hello", "features": []})
        assert tray.receive_json()["type"] == "hub"  # the messages before it were handled
    assert next(p for p in peers.load_peers() if p.name == "sage-dev").lan_ip == "192.168.1.20"


def test_settings_says_when_audio_goes_over_the_home_network(session):
    client = TestClient(app)
    assert "Tailscale is down" not in client.get("/hub").text
    peers.set_lan_mode(True)
    assert "Audio over the home network (Tailscale is down)" in client.get("/hub").text


def test_a_vban_rule_without_a_lan_address_names_only_tailscale(session):
    peers.sync_managed_conf()
    assert 'vban.ip = "100.64.0.13"' in conf()


def test_the_either_address_rule_matches_both_and_nothing_else():
    import re
    peer = peers.Peer(name="x", tailscale_ip="100.64.0.13", lan_ip="192.168.1.9", protocol="vban")
    pattern = peers.vban_sender(peer)[1:]
    assert re.fullmatch(pattern, "100.64.0.13") and re.fullmatch(pattern, "192.168.1.9")
    assert not re.fullmatch(pattern, "192.168.1.90") and not re.fullmatch(pattern, "100x64.0.13")
