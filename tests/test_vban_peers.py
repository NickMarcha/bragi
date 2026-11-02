"""VBAN peers on PipeWire's own vban-send/vban-recv modules.

They used to be vban_emitter/vban_receptor processes under a hand-written
host unit: every one registered as a client named "vban", so only one VBAN
peer could exist, and a pipewire.service restart left them writing to a dead
PulseAudio socket. As modules in peers.conf they get their own node names and
come back with the rest of the managed peers.
"""

from __future__ import annotations

import re

import pytest
import yaml
from fastapi.testclient import TestClient

from app import peers as peers_module
from app import views
from app.main import app


def conf() -> str:
    return peers_module.MANAGED_CONF_FILE.read_text()


def test_desktop_is_a_managed_vban_peer(session):
    desktop = next(p for p in peers_module.load_peers() if p.name == "desktop")
    assert desktop.protocol == "vban" and desktop.managed
    assert (desktop.tailscale_ip, desktop.vban_port) == ("100.64.0.13", 6980)
    assert (desktop.stream_send, desktop.stream_receive) == ("BragiMic", "DesktopAudio")
    assert (desktop.outgoing_sink_name, desktop.incoming_source_name) == ("desktop-outgoing-sink", "desktop-incoming-source")


def test_startup_migrates_a_hand_configured_vban_entry(session):
    """A peers.yaml written before the migration holds the old entry."""
    legacy = [{"name": "desktop", "protocol": "vban", "tailscale_ip": "100.64.0.13", "managed": False,
               "vban_port": 6980, "stream_send": "BragiMic", "stream_receive": "DesktopAudio"}]
    peers_module.PEERS_FILE.write_text(yaml.safe_dump(legacy))

    peers_module.sync_managed_conf()

    desktop = peers_module.load_peers()[0]
    assert desktop.managed and desktop.incoming_source_name == "desktop-incoming-source"
    assert 'sess.name = "BragiMic"' in conf()


def test_a_vban_peer_sends_the_mic_in_voicemeeters_format(session):
    peers_module.sync_managed_conf()

    text = conf()
    send = text[text.index("libpipewire-module-vban-send"):]
    assert "destination.ip = 100.64.0.13" in send
    assert "destination.port = 6980" in send
    assert 'sess.name = "BragiMic"' in send
    assert 'audio.format = "S16LE"' in send and "audio.rate = 48000" in send and "audio.channels = 2" in send
    assert 'node.name = "desktop-outgoing-sink"' in send
    # Without it the module is a capture stream that links to the default source.
    assert 'media.class = "Audio/Sink"' in send
    assert 'target.object = "desktop-outgoing-sink"' in text  # the same mic loopback Roc peers get
    assert 'node.name = "mic-to-desktop-capture"' in text


def test_vban_peers_share_one_receiver_that_names_each_stream(session):
    """VBAN multiplexes streams on one port, and only one socket can bind it."""
    peers_module.add_vban_peer("laptop", "100.64.0.14", "BragiMic", "LaptopAudio")

    text = conf()
    assert text.count("libpipewire-module-vban-recv") == 1
    assert "source.ip = 0.0.0.0" in text and "source.port = 6980" in text
    assert re.search(r'sess.name = "DesktopAudio"\s+vban.ip = "100.64.0.13"', text)
    assert re.search(r'sess.name = "LaptopAudio"\s+vban.ip = "100.64.0.14"', text)
    assert 'node.name = "laptop-incoming-source"' in text
    # A port clash (an old vban_receptor still running) must not stop the Roc peers.
    assert re.search(r"name = libpipewire-module-vban-recv\s+flags = \[ nofail \]", text)


def test_removing_a_vban_peer_drops_its_stream(session):
    peers_module.add_vban_peer("laptop", "100.64.0.14", "BragiMic", "LaptopAudio")
    peers_module.remove_peer("laptop")

    text = conf()
    assert "laptop" not in text and "LaptopAudio" not in text
    assert "DesktopAudio" in text


def test_removing_every_vban_peer_drops_the_receiver(session):
    peers_module.remove_peer("desktop")

    assert "vban" not in conf()


def test_two_peers_cannot_send_the_same_stream_name(session):
    """The receiver tells streams apart by name alone once they exist."""
    with pytest.raises(ValueError):
        peers_module.add_vban_peer("laptop", "100.64.0.14", "BragiMic", "DesktopAudio")


@pytest.mark.parametrize("stream", ['Bad"Name', "", "SeventeenCharName"])
def test_stream_names_cannot_inject_config(session, stream):
    with pytest.raises(ValueError):
        peers_module.add_vban_peer("laptop", "100.64.0.14", stream, "LaptopAudio")


def test_vban_nodes_are_found_by_name(session):
    desktop = next(p for p in peers_module.load_peers() if p.name == "desktop")
    graph = peers_module.pipewire.dump()
    assert views.resolve_node_id(graph, desktop, "outgoing") == 211
    assert views.resolve_node_id(graph, desktop, "incoming") == 220


def test_the_form_adds_a_vban_peer_with_default_stream_names(session):
    client = TestClient(app)
    response = client.post("/peers", data={"name": "Laptop", "tailscale_ip": "100.64.0.14", "protocol": "vban"})
    assert response.status_code == 200
    laptop = next(p for p in peers_module.load_peers() if p.name == "laptop")
    assert (laptop.protocol, laptop.stream_send, laptop.stream_receive) == ("vban", "BragiMic", "LaptopAudio")
    assert client.post("/peers", data={"name": "tablet", "tailscale_ip": "100.64.0.22"}).status_code == 200
    assert next(p for p in peers_module.load_peers() if p.name == "tablet").protocol == "roc"
