"""Peer registry: the source of truth for who Bragi bridges audio to/from.

Existing hand-written peers (sagedeck, sage-dev via Roc; sage via VBAN) are
seeded here to match what's already deployed on sagepi - Bragi controls
their volume like any other peer, but won't rewrite their config unless
you remove/re-add them through the UI. Peers added *through* Bragi get a
dedicated, Bragi-owned config file that's safe to regenerate freely.

Bragi never loads a managed peer's modules itself. It only writes that file
(MANAGED_CONF_FILE), a standalone PipeWire client config. On sagepi a
systemd --user unit runs it as its own process (`pipewire -c`, the way
filter-chain runs), and a path unit restarts that process whenever the
file changes - see host/systemd/. The earlier hot-load ran a one-shot
`pw-cli load-module`, whose modules die with the pw-cli process the moment
it exits, with no error, so a peer added from the dashboard never actually
reached the graph until the next pipewire.service restart.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from . import pipewire

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PEERS_FILE = DATA_DIR / "peers.yaml"
# In the data dir, so the container needs no mount beyond the one it has.
# Deliberately not in pipewire.conf.d: the main daemon would load it too,
# binding every managed peer's ports twice.
MANAGED_CONF_FILE = DATA_DIR / "peers.conf"

# First free port block after the hand-allocated 10001-10033 range
# documented in issue #061 (sagedeck/sage-dev). Each Roc peer needs 6 ports
# (mic source/repair/control, playback source/repair/control).
_PORT_BASE = 10041
_PORT_BLOCK_SIZE = 10

logger = logging.getLogger("bragi.peers")

_lock = threading.Lock()


@dataclass
class RocPorts:
    mic_source: int
    mic_repair: int
    mic_control: int
    playback_source: int
    playback_repair: int
    playback_control: int


@dataclass
class Peer:
    name: str
    protocol: str  # "roc" | "vban"
    tailscale_ip: str
    managed: bool = False  # True if Bragi owns this peer's config file
    # roc-specific
    ports: RocPorts | None = None
    outgoing_sink_name: str | None = None
    incoming_source_name: str | None = None
    # vban-specific
    vban_port: int | None = None
    stream_send: str | None = None
    stream_receive: str | None = None
    client_kind: str | None = None
    capture_mode: str = "microphone"
    send_enabled: bool = True
    receive_enabled: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _seed_peers() -> list[Peer]:
    """Mirrors the peers already hand-configured on sagepi as of #061."""
    return [
        Peer(
            name="sagedeck",
            protocol="roc",
            tailscale_ip="100.86.187.54",
            managed=False,
            ports=RocPorts(10001, 10002, 10003, 10011, 10012, 10013),
            outgoing_sink_name="sagedeck-test-sink",
            incoming_source_name="sagedeck-audio",
        ),
        Peer(
            name="sage-dev",
            protocol="roc",
            tailscale_ip="100.79.103.97",
            managed=False,
            ports=RocPorts(10021, 10022, 10023, 10031, 10032, 10033),
            outgoing_sink_name="sagedev-test-sink",
            incoming_source_name="sagedev-audio",
        ),
        Peer(
            name="sage",
            protocol="vban",
            tailscale_ip="100.71.149.116",
            managed=False,
            vban_port=6980,
            stream_send="SagepiMic",
            stream_receive="SageAudio",
        ),
    ]


def load_peers() -> list[Peer]:
    if not PEERS_FILE.exists():
        peers = _seed_peers()
        save_peers(peers)
        return peers
    raw = yaml.safe_load(PEERS_FILE.read_text()) or []
    peers = []
    for item in raw:
        ports = RocPorts(**item["ports"]) if item.get("ports") else None
        item = {**item, "ports": ports}
        peers.append(Peer(**item))
    return peers


def save_peers(peers: list[Peer]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw = [p.to_dict() for p in peers]
    PEERS_FILE.write_text(yaml.safe_dump(raw, sort_keys=False))


def _next_free_ports(existing: list[Peer]) -> RocPorts:
    used_bases = {
        p.ports.mic_source for p in existing if p.ports
    }
    base = _PORT_BASE
    while base in used_bases:
        base += _PORT_BLOCK_SIZE
    return RocPorts(
        mic_source=base,
        mic_repair=base + 1,
        mic_control=base + 2,
        playback_source=base + 3,
        playback_repair=base + 4,
        playback_control=base + 5,
    )


def _headset_mic_source_name() -> str | None:
    """The physical mic node currently feeding all peers' outgoing audio."""
    for node in pipewire.dump().nodes:
        if node.media_class == "Audio/Source" and "alsa_input" in node.name:
            return node.name
    return None


def _roc_conf_block(peer: Peer) -> str:
    assert peer.ports is not None
    mic_source_name = _headset_mic_source_name() or "alsa_input.MISSING"
    # node.dont-fallback + node.linger: without these, module-loopback's
    # hardcoded PW_STREAM_FLAG_AUTOCONNECT means that if mic_source_name
    # ever stops existing (disabled headset, reboot before the mic
    # reconnects), WirePlumber's linking/find-defined-target.lua falls
    # through to its normal default-source policy instead of just failing -
    # and on this box that picked a HEADSET'S OWN OUTPUT MONITOR as the
    # substitute "mic", silently routing whatever's playing on it (from
    # every connected peer) back out to every other peer, sage's VBAN mic
    # included. dont-fallback stops that fallback outright; linger keeps
    # the node alive waiting rather than destroying it, so it relinks on
    # its own the moment the real mic reappears - confirmed live on sagepi
    # (disable/re-enable headset via this dashboard) rather than assumed
    # from docs alone.
    return f"""\
    {{ name = libpipewire-module-roc-sink
      args = {{
          remote.ip = {peer.tailscale_ip}
          remote.source.port = {peer.ports.mic_source}
          remote.repair.port = {peer.ports.mic_repair}
          remote.control.port = {peer.ports.mic_control}
          fec.code = disable
          sink.name = "{peer.outgoing_sink_name}"
          sink.props = {{ node.name = "{peer.outgoing_sink_name}" node.description = "{peer.name} (Roc, via Bragi)" }}
      }}
    }}
    {{ name = libpipewire-module-roc-source
      args = {{
          local.ip = 0.0.0.0
          local.source.port = {peer.ports.playback_source}
          local.repair.port = {peer.ports.playback_repair}
          local.control.port = {peer.ports.playback_control}
          fec.code = disable
          sess.latency.msec = 40
          source.name = "{peer.incoming_source_name}"
          source.props = {{
              node.name = "{peer.incoming_source_name}"
              node.description = "{peer.name} (Roc, via Bragi)"
          }}
      }}
    }}
    {{ name = libpipewire-module-loopback
      args = {{
          capture.props = {{
              target.object = "{mic_source_name}"
              node.name = "mic-to-{peer.name}-capture"
              node.dont-fallback = true
              node.linger = true
          }}
          playback.props = {{
              target.object = "{peer.outgoing_sink_name}"
              node.name = "mic-to-{peer.name}-playback"
          }}
      }}
    }}
"""


class ConfigWriteError(RuntimeError):
    pass


# What `pipewire -c` needs to run as a client of the main daemon rather than
# a daemon of its own: the native protocol to reach it, client-node and
# adapter to create the stream nodes the Roc and loopback modules make. Same
# preamble as PipeWire's own filter-chain.conf. No core.daemon, which is
# what would make it a second daemon.
_CONF_PREAMBLE = """\
# Written by Bragi (app/peers.py) - edits here are overwritten.
context.properties = {
    log.level = 0
}
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt
      args = { nice.level = -11 }
      flags = [ ifexists nofail ]
    }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
"""


def _regenerate_managed_conf(peers: list[Peer]) -> None:
    managed = [p for p in peers if p.managed and p.protocol == "roc"]
    blocks = "\n".join(_roc_conf_block(p) for p in managed)
    content = f"{_CONF_PREAMBLE}{blocks}]\n"
    # Unchanged content is left alone: the path unit restarts every managed
    # peer on any write to this file, so writing it anyway would drop their
    # audio for nothing (sync_managed_conf runs on every Bragi startup).
    try:
        if MANAGED_CONF_FILE.exists() and MANAGED_CONF_FILE.read_text() == content:
            return
    except OSError:
        pass
    try:
        MANAGED_CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
        MANAGED_CONF_FILE.write_text(content)
    except OSError as exc:
        # The peer registry (peers.yaml) was already saved - surface this
        # clearly rather than a bare 500, since without the file the peer
        # never reaches the graph.
        raise ConfigWriteError(
            f"could not write {MANAGED_CONF_FILE} ({exc}) - the peer was saved "
            "but won't reach the audio graph until this is fixed"
        ) from exc


def sync_managed_conf() -> None:
    """Brings MANAGED_CONF_FILE in line with peers.yaml - run on startup, so
    a fresh install or a change to the template itself reaches sagepi
    without waiting for the next add or remove."""
    with _lock:
        peers = load_peers()
        # With no mic to name, every loopback would target a node that
        # doesn't exist (see _roc_conf_block) - starting while the headset
        # is disabled must not replace a file that names the real one.
        if any(p.managed for p in peers) and _headset_mic_source_name() is None:
            logger.warning("no headset mic in the graph - leaving %s as it is", MANAGED_CONF_FILE)
            return
        _regenerate_managed_conf(peers)


def add_roc_peer(name: str, tailscale_ip: str) -> Peer:
    with _lock:
        peers = load_peers()
        if any(p.name == name for p in peers):
            raise ValueError(f"peer '{name}' already exists")
        ports = _next_free_ports(peers)
        peer = Peer(
            name=name,
            protocol="roc",
            tailscale_ip=tailscale_ip,
            managed=True,
            ports=ports,
            outgoing_sink_name=f"{name}-outgoing-sink",
            incoming_source_name=f"{name}-incoming-source",
        )
        peers.append(peer)
        save_peers(peers)
        _regenerate_managed_conf(peers)
        return peer


def remove_peer(name: str) -> None:
    with _lock:
        peers = load_peers()
        peer = next((p for p in peers if p.name == name), None)
        if peer is None:
            raise ValueError(f"peer '{name}' not found")
        if not peer.managed:
            raise ValueError(
                f"'{name}' was hand-configured outside Bragi - remove it by editing "
                "sagepi's pipewire.conf.d directly, not through the UI"
            )
        peers = [p for p in peers if p.name != name]
        save_peers(peers)
        _regenerate_managed_conf(peers)


def register_android_peer(name: str, tailscale_ip: str, capture_mode: str) -> Peer:
    """Registration is repeatable: reconnecting must keep its port allocation."""
    with _lock:
        peers = load_peers()
        peer = next((p for p in peers if p.name == name), None)
        if peer is not None and (not peer.managed or peer.client_kind != "android"):
            raise ValueError(f"'{name}' already belongs to another client")
        if peer is None:
            peer = Peer(
                name=name, protocol="roc", tailscale_ip=tailscale_ip, managed=True,
                ports=_next_free_ports(peers), client_kind="android",
                outgoing_sink_name=f"{name}-outgoing-sink",
                incoming_source_name=f"{name}-incoming-source",
            )
            peers.append(peer)
        peer.tailscale_ip = tailscale_ip
        peer.capture_mode = capture_mode
        save_peers(peers)
        _regenerate_managed_conf(peers)
        return peer


def set_android_streams(name: str, send_enabled: bool, receive_enabled: bool) -> Peer:
    with _lock:
        peers = load_peers()
        peer = next((p for p in peers if p.name == name and p.client_kind == "android"), None)
        if peer is None:
            raise ValueError(f"Android peer '{name}' not found")
        peer.send_enabled = send_enabled
        peer.receive_enabled = receive_enabled
        save_peers(peers)
        # Only the app changes its streams. Restarting PipeWire peers here
        # would interrupt every managed peer for a control-plane action.
        return peer
