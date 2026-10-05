"""Adding and removing Roc peers from the dashboard.

Bragi doesn't load a managed peer's modules itself any more. A module loaded
by a one-shot `pw-cli load-module` dies with that process, so the old
hot-load looked like it worked and left nothing in the graph. Bragi now only
writes peers.conf, a standalone PipeWire client config. On sagepi a
systemd --user unit runs it (`pipewire -c`), and a path unit restarts it
when the file changes (see host/systemd/). These tests cover the file;
what the units do with it can only be checked on the Pi.
"""

from __future__ import annotations

import re

import pytest

from app import peers as peers_module

from .fake_pipewire import OFF_PROFILE


def conf() -> str:
    return peers_module.MANAGED_CONF_FILE.read_text()


def test_adding_a_peer_writes_its_modules_to_the_managed_conf(session):
    peers_module.add_roc_peer("fairphone", "100.98.253.67")

    text = conf()
    assert "libpipewire-module-roc-sink" in text
    assert "libpipewire-module-roc-source" in text
    assert 'node.name = "mic-to-fairphone-capture"' in text
    assert "remote.ip = 100.98.253.67" in text


def test_adding_a_peer_never_loads_modules_through_a_one_shot_pw_cli(session):
    """The bug this replaces: the module lives in pw-cli's own connection
    and is destroyed the moment that process exits, with no error."""
    peers_module.add_roc_peer("fairphone", "100.98.253.67")

    assert session.count("pw-cli", "load-module") == 0


def test_a_peers_incoming_stream_keeps_its_default_media_class(session):
    """Forcing Audio/Source made WirePlumber treat the incoming stream as a
    mic, which nothing auto-links, so the peer was silent in the headset.
    Hand-configured peers that work are Stream/Output/Audio, the module's
    own default."""
    peers_module.add_roc_peer("fairphone", "100.98.253.67")

    text = conf()
    roc_source = text[text.index("libpipewire-module-roc-source"):text.index("mic-to-fairphone-capture")]
    assert "media.class" not in roc_source


def test_the_conf_runs_as_its_own_pipewire_client(session):
    """`pipewire -c` on a file with only peer modules in it would have no
    protocol to reach the main daemon or create stream nodes with."""
    peers_module.add_roc_peer("fairphone", "100.98.253.67")

    text = conf()
    for module in ("libpipewire-module-protocol-native", "libpipewire-module-client-node", "libpipewire-module-adapter"):
        assert module in text
    assert "core.daemon" not in text  # a client of the main daemon, never a second one


def test_the_mic_loopback_still_refuses_to_fall_back(session):
    """#086: without these, a missing mic was replaced by a headset output
    monitor and everyone heard everyone else's audio as sagepi's mic."""
    peers_module.add_roc_peer("fairphone", "100.98.253.67")

    text = conf()
    assert "node.dont-fallback = true" in text
    assert "node.linger = true" in text


def test_removing_a_peer_drops_it_from_the_conf(session):
    peers_module.add_roc_peer("fairphone", "100.98.253.67")
    peers_module.add_roc_peer("tablet", "100.98.253.68")

    peers_module.remove_peer("fairphone")

    text = conf()
    assert "fairphone" not in text
    assert "mic-to-tablet-capture" in text
    assert [p.name for p in peers_module.load_peers()] == ["sagedeck", "sage-dev", "sage", "tablet"]


def test_removing_the_last_managed_peer_leaves_a_runnable_conf(session):
    """The unit keeps running with nothing to bridge rather than failing on
    an empty or missing file."""
    peers_module.add_roc_peer("fairphone", "100.98.253.67")
    peers_module.remove_peer("fairphone")

    text = conf()
    assert "libpipewire-module-protocol-native" in text
    assert "roc-sink" not in text


def test_each_added_peer_gets_its_own_port_block(session):
    a = peers_module.add_roc_peer("fairphone", "100.98.253.67")
    b = peers_module.add_roc_peer("tablet", "100.98.253.68")

    assert a.ports.mic_source == 10041
    assert b.ports.mic_source == 10051
    roc_ports = set(re.findall(r"port = (\d+)", conf())) - {str(peers_module.VBAN_PORT)}
    assert len(roc_ports) == 12


def test_hand_configured_peers_stay_out_of_the_managed_conf(session):
    """They're loaded by sagepi's own pipewire.conf.d. Writing them here too
    would bind their ports twice."""
    peers_module.add_roc_peer("fairphone", "100.98.253.67")

    text = conf()
    for name in ("sagedeck", "sagedev", "sage-dev"):
        assert name not in text


def test_a_hand_configured_peer_cannot_be_removed(session):
    with pytest.raises(ValueError):
        peers_module.remove_peer("sagedeck")


def test_startup_writes_the_conf_once_and_then_leaves_it_alone(session):
    """Bragi rewrites the file on startup so a fresh install, or a change
    to the template itself, reaches sagepi. But the path unit restarts the
    managed peers on every write, so a redeploy that changes nothing about
    them mustn't touch the file."""
    peers_module.add_roc_peer("fairphone", "100.98.253.67")
    peers_module.MANAGED_CONF_FILE.unlink()

    peers_module.sync_managed_conf()
    first = peers_module.MANAGED_CONF_FILE.stat().st_mtime_ns
    assert "mic-to-fairphone-capture" in conf()

    peers_module.sync_managed_conf()
    assert peers_module.MANAGED_CONF_FILE.stat().st_mtime_ns == first


def test_startup_with_the_headset_off_keeps_the_last_good_conf(session):
    """The loopbacks name the headset mic, looked up when the file is
    written. Starting while the headset is disabled would otherwise point
    every peer's mic feed at a node that doesn't exist."""
    peers_module.add_roc_peer("fairphone", "100.98.253.67")
    before = conf()
    for card in session.cards:
        card.active_index = OFF_PROFILE

    peers_module.sync_managed_conf()

    assert conf() == before
