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


def test_sage_is_seeded_as_a_managed_vban_peer(session):
    sage = next(p for p in peers_module.load_peers() if p.name == "sage")
    assert sage.protocol == "vban" and sage.managed
    assert (sage.tailscale_ip, sage.vban_port) == ("100.71.149.116", 6980)
    assert (sage.stream_send, sage.stream_receive) == ("SagepiMic", "SageAudio")
    assert (sage.outgoing_sink_name, sage.incoming_source_name) == ("sage-outgoing-sink", "sage-incoming-source")


def test_startup_migrates_the_hand_configured_sage_entry(session):
    """sagepi's peers.yaml still holds the pre-migration entry."""
    legacy = [{"name": "sage", "protocol": "vban", "tailscale_ip": "100.71.149.116", "managed": False,
               "vban_port": 6980, "stream_send": "SagepiMic", "stream_receive": "SageAudio"}]
    peers_module.PEERS_FILE.write_text(yaml.safe_dump(legacy))

    peers_module.sync_managed_conf()

    sage = peers_module.load_peers()[0]
    assert sage.managed and sage.incoming_source_name == "sage-incoming-source"
    assert 'sess.name = "SagepiMic"' in conf()


def test_a_vban_peer_sends_the_mic_in_voicemeeters_format(session):
    peers_module.sync_managed_conf()

    text = conf()
    send = text[text.index("libpipewire-module-vban-send"):]
    assert "destination.ip = 100.71.149.116" in send
    assert "destination.port = 6980" in send
    assert 'sess.name = "SagepiMic"' in send
    assert 'audio.format = "S16LE"' in send and "audio.rate = 48000" in send and "audio.channels = 2" in send
    assert 'node.name = "sage-outgoing-sink"' in send
    # Without it the module is a capture stream that links to the default source.
    assert 'media.class = "Audio/Sink"' in send
    assert 'target.object = "sage-outgoing-sink"' in text  # the same mic loopback Roc peers get
    assert 'node.name = "mic-to-sage-capture"' in text


def test_vban_peers_share_one_receiver_that_names_each_stream(session):
    """VBAN multiplexes streams on one port, and only one socket can bind it."""
    peers_module.add_vban_peer("laptop", "100.71.149.117", "SagepiMic", "LaptopAudio")

    text = conf()
    assert text.count("libpipewire-module-vban-recv") == 1
    assert "source.ip = 0.0.0.0" in text and "source.port = 6980" in text
    assert re.search(r'sess.name = "SageAudio"\s+vban.ip = "100.71.149.116"', text)
    assert re.search(r'sess.name = "LaptopAudio"\s+vban.ip = "100.71.149.117"', text)
    assert 'node.name = "laptop-incoming-source"' in text
    # A port clash (an old vban_receptor still running) must not stop the Roc peers.
    assert re.search(r"name = libpipewire-module-vban-recv\s+flags = \[ nofail \]", text)


def test_removing_a_vban_peer_drops_its_stream(session):
    peers_module.add_vban_peer("laptop", "100.71.149.117", "SagepiMic", "LaptopAudio")
    peers_module.remove_peer("laptop")

    text = conf()
    assert "laptop" not in text and "LaptopAudio" not in text
    assert "SageAudio" in text


def test_removing_every_vban_peer_drops_the_receiver(session):
    peers_module.remove_peer("sage")

    assert "vban" not in conf()


def test_two_peers_cannot_send_the_same_stream_name(session):
    """The receiver tells streams apart by name alone once they exist."""
    with pytest.raises(ValueError):
        peers_module.add_vban_peer("laptop", "100.71.149.117", "SagepiMic", "SageAudio")


@pytest.mark.parametrize("stream", ['Bad"Name', "", "SeventeenCharName"])
def test_stream_names_cannot_inject_config(session, stream):
    with pytest.raises(ValueError):
        peers_module.add_vban_peer("laptop", "100.71.149.117", stream, "LaptopAudio")


def test_vban_nodes_are_found_by_name(session):
    sage = next(p for p in peers_module.load_peers() if p.name == "sage")
    graph = peers_module.pipewire.dump()
    assert views.resolve_node_id(graph, sage, "outgoing") == 211
    assert views.resolve_node_id(graph, sage, "incoming") == 220


def test_the_form_adds_a_vban_peer_with_default_stream_names(session):
    client = TestClient(app)
    response = client.post("/peers", data={"name": "Laptop", "tailscale_ip": "100.71.149.117", "protocol": "vban"})
    assert response.status_code == 200
    laptop = next(p for p in peers_module.load_peers() if p.name == "laptop")
    assert (laptop.protocol, laptop.stream_send, laptop.stream_receive) == ("vban", "SagepiMic", "LaptopAudio")
    assert client.post("/peers", data={"name": "tablet", "tailscale_ip": "100.98.253.68"}).status_code == 200
    assert next(p for p in peers_module.load_peers() if p.name == "tablet").protocol == "roc"
