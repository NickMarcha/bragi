"""Peer registry: the source of truth for who Bragi bridges audio to/from.

Existing hand-written peers (sagedeck, sage-dev via Roc; sage via VBAN) are
seeded here to match what's already deployed on sagepi - Bragi controls
their volume like any other peer, but won't rewrite their config unless
you remove/re-add them through the UI. Peers added *through* Bragi get a
dedicated, Bragi-owned config file that's safe to regenerate freely.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from . import pipewire

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PEERS_FILE = DATA_DIR / "peers.yaml"
MANAGED_CONF_FILE = Path(
    "/host-pipewire-conf.d/70-bragi-peers.conf"
)  # bind-mounted, see Dockerfile/README

# First free port block after the hand-allocated 10001-10033 range
# documented in issue #061 (sagedeck/sage-dev). Each Roc peer needs 6 ports
# (mic source/repair/control, playback source/repair/control).
_PORT_BASE = 10041
_PORT_BLOCK_SIZE = 10

_lock = threading.Lock()
_live_module_ids: dict[str, list[int]] = {}  # peer name -> module ids loaded this session


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
    for node in pipewire.list_nodes():
        if node.media_class == "Audio/Source" and "alsa_input" in node.name:
            return node.name
    return None


def _roc_conf_block(peer: Peer) -> str:
    assert peer.ports is not None
    mic_source_name = _headset_mic_source_name() or "alsa_input.MISSING"
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
              media.class = "Audio/Source"
          }}
      }}
    }}
    {{ name = libpipewire-module-loopback
      args = {{
          capture.props = {{
              target.object = "{mic_source_name}"
              node.name = "mic-to-{peer.name}-capture"
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


def _regenerate_managed_conf(peers: list[Peer]) -> None:
    managed = [p for p in peers if p.managed and p.protocol == "roc"]
    blocks = "\n".join(_roc_conf_block(p) for p in managed)
    content = f"context.modules = [\n{blocks}]\n"
    try:
        MANAGED_CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
        MANAGED_CONF_FILE.write_text(content)
    except OSError as exc:
        # Most likely the pipewire.conf.d bind mount is missing/misconfigured
        # (or this is local dev without it). The peer registry (peers.yaml)
        # and any hot-load already succeeded/failed independently of this -
        # surface it clearly rather than a bare 500.
        raise ConfigWriteError(
            f"could not write {MANAGED_CONF_FILE} ({exc}) - the peer was still "
            "saved and hot-loaded if possible, but won't survive a "
            "pipewire.service restart until this is fixed"
        ) from exc


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
        _hot_load_peer(peer)
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
        _hot_unload_peer(peer)
        peers = [p for p in peers if p.name != name]
        save_peers(peers)
        _regenerate_managed_conf(peers)


def _hot_load_peer(peer: Peer) -> None:
    assert peer.ports is not None
    mic_source_name = _headset_mic_source_name()
    module_ids = []
    try:
        module_ids.append(
            pipewire.load_module(
                "libpipewire-module-roc-sink",
                {
                    "remote.ip": peer.tailscale_ip,
                    "remote.source.port": peer.ports.mic_source,
                    "remote.repair.port": peer.ports.mic_repair,
                    "remote.control.port": peer.ports.mic_control,
                    "fec.code": "disable",
                    "sink.name": peer.outgoing_sink_name,
                    "sink.props": {
                        "node.name": peer.outgoing_sink_name,
                        "node.description": f"{peer.name} (Roc, via Bragi)",
                    },
                },
            )
        )
        module_ids.append(
            pipewire.load_module(
                "libpipewire-module-roc-source",
                {
                    "local.ip": "0.0.0.0",
                    "local.source.port": peer.ports.playback_source,
                    "local.repair.port": peer.ports.playback_repair,
                    "local.control.port": peer.ports.playback_control,
                    "fec.code": "disable",
                    "sess.latency.msec": 40,
                    "source.name": peer.incoming_source_name,
                    "source.props": {
                        "node.name": peer.incoming_source_name,
                        "node.description": f"{peer.name} (Roc, via Bragi)",
                        "media.class": "Audio/Source",
                    },
                },
            )
        )
        if mic_source_name:
            module_ids.append(
                pipewire.load_module(
                    "libpipewire-module-loopback",
                    {
                        "capture.props": {
                            "target.object": mic_source_name,
                            "node.name": f"mic-to-{peer.name}-capture",
                        },
                        "playback.props": {
                            "target.object": peer.outgoing_sink_name,
                            "node.name": f"mic-to-{peer.name}-playback",
                        },
                    },
                )
            )
        _live_module_ids[peer.name] = module_ids
    except pipewire.PipewireError:
        # Hot-load failed (e.g. Bragi can't reach the PipeWire socket) - the
        # config file is still written, so it'll come up on the next
        # pipewire.service restart even though this attempt didn't apply live.
        pass


def _hot_unload_peer(peer: Peer) -> None:
    for module_id in _live_module_ids.pop(peer.name, []):
        try:
            pipewire.unload_module(module_id)
        except pipewire.PipewireError:
            pass
