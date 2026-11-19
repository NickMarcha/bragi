"""Letting Bragi manage hand-configured Roc peers (bragi#62). The demo
peers "deck" and "studio" are managed: false; their modules live in a
hand-written conf.d file like sagehub's 60-roc-bridge.conf."""

from __future__ import annotations

import pytest

from app import audio_reset, peers, setup_checks

HAND_CONF = """context.modules = [
    { name = libpipewire-module-roc-sink
      args = { remote.ip = 100.64.0.12 remote.source.port = 10001 remote.repair.port = 10002 remote.control.port = 10003
               sink.name = "deck-test-sink" sink.props = { node.name = "deck-test-sink" } } }
    { name = libpipewire-module-roc-source
      args = { local.ip = 0.0.0.0 local.source.port = 10011 local.repair.port = 10012 local.control.port = 10013
               source.name = "deck-audio" source.props = { node.name = "deck-audio" } } }
    { name = libpipewire-module-roc-sink
      args = { remote.ip = 100.64.0.13 sink.name = "studio-test-sink" sink.props = { node.name = "studio-test-sink" } } }
    { name = libpipewire-module-roc-source
      args = { source.name = "studio-audio" source.props = { node.name = "studio-audio" } } }
    { name = libpipewire-module-loopback
      args = { capture.props = { node.name = "mic-to-deck-capture" } playback.props = { target.object = "deck-test-sink" } } }
]
"""


@pytest.fixture
def confd(tmp_path, monkeypatch, session):
    d = tmp_path / "pipewire.conf.d"
    d.mkdir()
    (d / "60-roc-bridge.conf").write_text(HAND_CONF)
    (d / "10-unrelated.conf").write_text('context.properties = { default.clock.rate = 48000 }\n')
    monkeypatch.setattr(peers, "PIPEWIRE_CONF_DIR", d)
    return d


def test_the_hand_written_file_is_found(confd):
    report = peers.hand_configured()
    assert report["peers"] == ["deck", "studio"]
    assert report["files"] == [{"path": str(confd / "60-roc-bridge.conf"), "peers": ["deck", "studio"], "other": []}]
    check = setup_checks.check_hand_peers()
    assert check["status"] == "problem" and check["fix"]["action"] == "adopt-peers"
    assert "60-roc-bridge.conf" in check["detail"]


def test_adopting_keeps_ports_and_names_sets_the_file_aside_and_resets_audio(confd):
    before = {p.name: (p.ports, p.outgoing_sink_name, p.incoming_source_name) for p in peers.load_peers()}
    note = setup_checks.fix("handpeers")
    assert "Bragi manages deck, studio now" in note
    after = {p.name: p for p in peers.load_peers()}
    assert after["deck"].managed and after["studio"].managed
    for name in ("deck", "studio"):
        assert (after[name].ports, after[name].outgoing_sink_name, after[name].incoming_source_name) == before[name]
    conf = peers.MANAGED_CONF_FILE.read_text()
    assert 'sink.name = "deck-test-sink"' in conf and 'source.name = "studio-audio"' in conf
    assert "remote.source.port = 10001" in conf
    assert not (confd / "60-roc-bridge.conf").exists()
    assert list(confd.glob("60-roc-bridge.conf.adopted-*"))
    assert (confd / "10-unrelated.conf").exists()
    assert audio_reset.view()["state"] == "pending"
    assert setup_checks.check_hand_peers()["status"] == "na"


def test_a_file_with_modules_bragi_doesnt_know_is_left_alone(confd):
    (confd / "60-roc-bridge.conf").write_text(HAND_CONF.replace(
        "]\n", '    { name = libpipewire-module-roc-sink args = { sink.name = "mystery-sink" } }\n]\n'))
    check = setup_checks.check_hand_peers()
    assert check["status"] == "problem" and check["fix"] is None and "mystery-sink" in check["detail"]
    with pytest.raises(ValueError, match="mystery-sink"):
        peers.adopt_hand_configured()
    assert not any(p.managed for p in peers.load_peers() if p.name in ("deck", "studio"))
    assert (confd / "60-roc-bridge.conf").exists()


def test_peers_configured_somewhere_else_get_no_fix(confd):
    (confd / "60-roc-bridge.conf").unlink()
    check = setup_checks.check_hand_peers()
    assert check["status"] == "problem" and check["fix"] is None and "can't take them over" in check["detail"]
