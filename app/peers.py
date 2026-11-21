"""Peer registry: the source of truth for who Bragi bridges audio to/from.

A fresh install starts with no peers. Peers added *through* Bragi get a
dedicated, Bragi-owned config file that's safe to regenerate freely.
Roc peers you already run from your own pipewire.conf.d can be listed in
data/peers.yaml with `managed: false` - Bragi controls their volume like
any other peer, but never writes their config (see docs/deployment.md).

Bragi never loads a managed peer's modules itself. It only writes that file
(MANAGED_CONF_FILE), a standalone PipeWire client config. On the host a
systemd --user unit runs it as its own process (`pipewire -c`, the way
filter-chain runs), and a path unit restarts that process whenever the
file changes - see host/systemd/. The earlier hot-load ran a one-shot
`pw-cli load-module`, whose modules die with the pw-cli process the moment
it exits, with no error, so a peer added from the dashboard never actually
reached the graph until the next pipewire.service restart.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from . import pipewire

# BRAGI_DATA_DIR keeps a local dev server (dev/server.py) off the repo's data/.
DATA_DIR = Path(os.environ.get("BRAGI_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
PEERS_FILE = DATA_DIR / "peers.yaml"
# In the data dir, so the container needs no mount beyond the one it has.
# Deliberately not in pipewire.conf.d: the main daemon would load it too,
# binding every managed peer's ports twice.
MANAGED_CONF_FILE = DATA_DIR / "peers.conf"

# First free port block after 10001-10033, left for Roc peers configured by
# hand in pipewire.conf.d. Each Roc peer needs 6 ports
# (mic source/repair/control, playback source/repair/control).
# BRAGI_ROC_PORT_BASE moves local dev peers into a free port range.
_PORT_BASE = int(os.environ.get("BRAGI_ROC_PORT_BASE", "10041"))
_PORT_BLOCK_SIZE = 10

# VBAN multiplexes streams by name on one port; Voicemeeter's default.
VBAN_PORT = 6980
# VBAN stream names are at most 16 bytes on the wire, and land in peers.conf.
_VBAN_STREAM_NAME = re.compile(r"^[A-Za-z0-9_-]{1,16}$")

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
    # The computer's home-network address, as its tray app reports it
    # (bragi#81). Only used while this hub's Tailscale is down.
    lan_ip: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["lan_ip"] is None:
            # Left out, so a peers.yaml without LAN addresses reads the same
            # in a Bragi from before they existed.
            del d["lan_ip"]
        return d


# Set by lan_fallback.py while this hub has no Tailscale address (bragi#81).
_use_lan = False

_LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_lan_ipv4(text: object) -> bool:
    """A private (RFC 1918) IPv4 address: what a tray app may report as its
    home-network address. Tailscale's 100.64.0.0/10 is not one of them."""
    try:
        address = ipaddress.ip_address(text) if isinstance(text, str) else None
    except ValueError:
        return False
    return address is not None and address.version == 4 and any(address in n for n in _LAN_NETWORKS)


def target_ip(peer: Peer) -> str:
    """Where this peer's audio goes, and comes from: its Tailscale address,
    or its LAN address while this hub's Tailscale is down and the peer
    reported one. Phones stay on Tailscale."""
    if _use_lan and peer.lan_ip and peer.client_kind != "android":
        return peer.lan_ip
    return peer.tailscale_ip


def using_lan() -> bool:
    return _use_lan


def _vban_peer(name: str, tailscale_ip: str, stream_send: str, stream_receive: str) -> Peer:
    return Peer(
        name=name, protocol="vban", tailscale_ip=tailscale_ip, managed=True,
        outgoing_sink_name=f"{name}-outgoing-sink", incoming_source_name=f"{name}-incoming-source",
        vban_port=VBAN_PORT, stream_send=stream_send, stream_receive=stream_receive,
    )


def _migrate_vban_peers(peers: list[Peer]) -> bool:
    """VBAN peers once ran on vban_emitter/vban_receptor under a hand-written
    unit, stored with managed: false, until they moved into peers.conf. Their
    stream names and addresses carry over unchanged."""
    changed = False
    for i, peer in enumerate(peers):
        if peer.protocol == "vban" and not peer.managed:
            peers[i] = _vban_peer(peer.name, peer.tailscale_ip, peer.stream_send, peer.stream_receive)
            changed = True
    return changed


def load_peers() -> list[Peer]:
    if not PEERS_FILE.exists():
        return []
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
    """The main headset's mic (headset_choice.py: Settings, or a HyperX
    dongle, or the first USB card), the loopbacks' starting target."""
    from . import headset_choice  # it imports this module
    return headset_choice.input_name()


def _headset_playback_name() -> str | None:
    """The main headset's output, which phones in listen mode hear."""
    from . import headset_choice
    return headset_choice.output_name()


def _roc_conf_block(peer: Peer) -> str:
    assert peer.ports is not None
    mic_source_name = _headset_mic_source_name() or "alsa_input.MISSING"
    android = peer.client_kind == "android"
    capture_target = (_headset_playback_name() or "alsa_output.MISSING") if android else mic_source_name
    capture_monitor = "stream.capture.sink = true" if android else ""
    source_kind = 'media.class = "Audio/Source" node.virtual = true' if android and peer.capture_mode == "microphone" else ""
    # node.dont-fallback + node.linger: without these, module-loopback's
    # hardcoded PW_STREAM_FLAG_AUTOCONNECT means that if mic_source_name
    # ever stops existing (disabled headset, reboot before the mic
    # reconnects), WirePlumber's linking/find-defined-target.lua falls
    # through to its normal default-source policy instead of just failing -
    # and on this box that picked a HEADSET'S OWN OUTPUT MONITOR as the
    # substitute "mic", silently routing whatever's playing on it (from
    # every connected peer) back out to every other peer, VBAN peers'
    # mics included. dont-fallback stops that fallback outright; linger keeps
    # the node alive waiting rather than destroying it, so it relinks on
    # its own the moment the real mic reappears - confirmed live on a Pi
    # (disable/re-enable headset via this dashboard) rather than assumed
    # from docs alone.
    return f"""\
    {{ name = libpipewire-module-roc-sink
      args = {{
          remote.ip = {target_ip(peer)}
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
              {source_kind}
          }}
      }}
    }}
{_mic_loopback_block(peer, capture_target, capture_monitor)}"""


def _mic_loopback_block(peer: Peer, capture_target: str, capture_monitor: str = "") -> str:
    """Feeds the peer's outgoing sink. See _roc_conf_block for why the
    capture side must never fall back to another source."""
    return f"""\
    {{ name = libpipewire-module-loopback
      args = {{
          capture.props = {{
              target.object = "{capture_target}"
              {capture_monitor}
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


def _vban_send_block(peer: Peer) -> str:
    # 48 kHz 16-bit stereo is what Voicemeeter expects from a VBAN emitter.
    mic_source_name = _headset_mic_source_name() or "alsa_input.MISSING"
    return f"""\
    {{ name = libpipewire-module-vban-send
      args = {{
          destination.ip = {target_ip(peer)}
          destination.port = {peer.vban_port}
          sess.name = "{peer.stream_send}"
          audio.format = "S16LE"
          audio.rate = 48000
          audio.channels = 2
          audio.position = [ FL FR ]
          stream.props = {{
              # Without it the module makes a capture stream that WirePlumber
              # links straight to the default source, bypassing the loopback.
              media.class = "Audio/Sink"
              node.name = "{peer.outgoing_sink_name}"
              node.description = "{peer.name} (VBAN, via Bragi)"
          }}
      }}
    }}
{_mic_loopback_block(peer, mic_source_name)}"""


def _vban_recv_block(peers: list[Peer]) -> str:
    """One receiver per port: VBAN multiplexes by stream name, and only one
    socket can bind the port. Each rule names one peer's stream; streams
    from anyone else are ignored. nofail, because a port clash (an old
    vban_receptor still running) must not stop the Roc peers starting."""
    (port,) = {p.vban_port for p in peers}
    rules = "".join(f"""
              {{ matches = [ {{ sess.name = "{p.stream_receive}"
                               vban.ip = "{target_ip(p)}" }} ]
                actions = {{ create-stream = {{
                    node.name = "{p.incoming_source_name}"
                    node.description = "{p.name} (VBAN, via Bragi)"
                }} }}
              }}""" for p in peers)
    return f"""\
    {{ name = libpipewire-module-vban-recv
      flags = [ nofail ]
      args = {{
          source.ip = 0.0.0.0
          source.port = {port}
          sess.latency.msec = 40
          stream.rules = [{rules}
          ]
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
    vban = [p for p in peers if p.managed and p.protocol == "vban"]
    blocks = "\n".join([_roc_conf_block(p) for p in managed] + [_vban_send_block(p) for p in vban]
                       + ([_vban_recv_block(vban)] if vban else []))
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
    a fresh install or a change to the template itself reaches the host
    without waiting for the next add or remove."""
    with _lock:
        peers = load_peers()
        if _migrate_vban_peers(peers):
            save_peers(peers)
        # With no mic to name, every loopback would target a node that
        # doesn't exist (see _roc_conf_block) - starting while the headset
        # is disabled must not replace a file that names the real one.
        if any(p.managed for p in peers) and _headset_mic_source_name() is None:
            logger.warning("no headset mic in the graph - leaving %s as it is", MANAGED_CONF_FILE)
            return
        _regenerate_managed_conf(peers)


def set_lan_mode(on: bool) -> bool:
    """Rewrites the managed config with LAN addresses (on) or Tailscale
    addresses (off); the path unit restarts the peers once if that changed
    the file. False, with nothing changed, while there's no headset mic to
    name (see sync_managed_conf): the caller tries again later."""
    global _use_lan
    with _lock:
        peers = load_peers()
        if any(p.managed for p in peers) and _headset_mic_source_name() is None:
            return False
        before, _use_lan = _use_lan, on
        try:
            _regenerate_managed_conf(peers)
        except ConfigWriteError:
            _use_lan = before
            raise
        return True


def set_lan_ip(name: str, lan_ip: str | None) -> bool:
    """Stores the LAN address a desktop peer's tray app reported (None
    clears it). True when it changed. Rewrites the config only while the
    LAN addresses are in use."""
    if lan_ip is not None and not is_lan_ipv4(lan_ip):
        raise ValueError(f"{lan_ip!r} is not a private IPv4 address")
    with _lock:
        peers = load_peers()
        peer = next((p for p in peers if p.name == name), None)
        if peer is None or not peer.managed or peer.client_kind == "android" or peer.lan_ip == lan_ip:
            return False
        peer.lan_ip = lan_ip
        save_peers(peers)
        if _use_lan:
            _regenerate_managed_conf(peers)
        return True


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


def add_vban_peer(name: str, tailscale_ip: str, stream_send: str, stream_receive: str) -> Peer:
    for stream in (stream_send, stream_receive):
        if not _VBAN_STREAM_NAME.match(stream):
            raise ValueError("VBAN stream names are 1-16 letters, digits, '-' or '_'")
    with _lock:
        peers = load_peers()
        if any(p.name == name for p in peers):
            raise ValueError(f"peer '{name}' already exists")
        # Once a stream exists, vban-recv looks it up by name alone.
        if any(p.protocol == "vban" and p.stream_receive == stream_receive for p in peers):
            raise ValueError(f"another VBAN peer already sends a stream named '{stream_receive}'")
        peer = _vban_peer(name, tailscale_ip, stream_send, stream_receive)
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
                "the host's pipewire.conf.d directly, not through the UI"
            )
        peers = [p for p in peers if p.name != name]
        save_peers(peers)
        _regenerate_managed_conf(peers)


# Where hand-written PipeWire configs live (the user's own conf.d).
PIPEWIRE_CONF_DIR = Path(os.environ.get("BRAGI_PIPEWIRE_CONF_DIR")
                         or Path.home() / ".config" / "pipewire" / "pipewire.conf.d")
_NODE_NAME = re.compile(r'\b(sink|source)\.name\s*=\s*"([^"]+)"')


def hand_configured() -> dict:
    """Roc peers configured by hand (managed: false) and the conf.d files
    that run them (bragi#62). A file is adoptable when every Roc sink and
    source in it belongs to one of these peers: then Bragi can take over all
    of them and set the file aside. A file with anything else is left alone."""
    unmanaged = [p for p in load_peers() if not p.managed and p.protocol == "roc"]
    files = []
    found: set[str] = set()
    if unmanaged and PIPEWIRE_CONF_DIR.is_dir():
        for path in sorted(PIPEWIRE_CONF_DIR.glob("*.conf")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            names = {name for _, name in _NODE_NAME.findall(text)}
            mine = [p for p in unmanaged if p.outgoing_sink_name in names or p.incoming_source_name in names]
            if not mine:
                continue
            known = {n for p in mine for n in (p.outgoing_sink_name, p.incoming_source_name)}
            files.append({"path": str(path), "peers": [p.name for p in mine], "other": sorted(names - known)})
            found.update(p.name for p in mine)
    return {"peers": [p.name for p in unmanaged], "files": files,
            "elsewhere": [p.name for p in unmanaged if p.name not in found]}


def adopt_hand_configured() -> list[str]:
    """Lets Bragi manage the hand-configured Roc peers whose conf.d files it
    can take over whole: they keep their ports and node names (so the other
    end needs no change), Bragi writes them into its managed config, and each
    file is renamed to <name>.adopted-<date>. PipeWire has to restart before
    the managed config can bind the ports the file's modules hold; the
    caller asks for an audio reset. Returns the adopted peers' names."""
    from datetime import date
    report = hand_configured()
    blocked = [f for f in report["files"] if f["other"]]
    if blocked:
        f = blocked[0]
        raise ValueError(f"{f['path']} also runs {', '.join(f['other'])}, which Bragi doesn't know. "
                         "Move those peers' modules to their own file, or list them in peers.yaml.")
    names = {n for f in report["files"] for n in f["peers"]}
    if not names:
        return []
    with _lock:
        peers = load_peers()
        for peer in peers:
            if peer.name in names:
                peer.managed = True
        save_peers(peers)
        _regenerate_managed_conf(peers)
    stamp = date.today().strftime("%Y%m%d")
    for f in report["files"]:
        path = Path(f["path"])
        path.rename(path.with_name(f"{path.name}.adopted-{stamp}"))
    logger.info("adopted hand-configured peers %s", ", ".join(sorted(names)))
    return sorted(names)


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
        if capture_mode != peer.capture_mode and "none" in (capture_mode, peer.capture_mode):
            # A listen-only phone has nothing to send; switching back restores sending.
            peer.send_enabled = capture_mode != "none"
            peer.receive_enabled = capture_mode == "none"
        peer.capture_mode = capture_mode
        save_peers(peers)
        _regenerate_managed_conf(peers)
        return peer


def register_desktop_peer(name: str, tailscale_ip: str) -> Peer:
    """The Bragi Client's "Connect to Bragi" window. Repeatable, like the
    Android registration: connecting again keeps the port block. A Roc peer
    added from the dashboard under the same name is claimed rather than
    refused, since adding it there first is the natural order. A peer
    configured by hand is returned as it is, never rewritten."""
    with _lock:
        peers = load_peers()
        peer = next((p for p in peers if p.name == name), None)
        if peer is not None and (peer.protocol != "roc" or peer.client_kind not in (None, "desktop")):
            raise ValueError(f"'{name}' already belongs to another client")
        if peer is not None and not peer.managed:
            return peer
        if peer is None:
            peer = Peer(
                name=name, protocol="roc", tailscale_ip=tailscale_ip, managed=True,
                ports=_next_free_ports(peers),
                outgoing_sink_name=f"{name}-outgoing-sink",
                incoming_source_name=f"{name}-incoming-source",
            )
            peers.append(peer)
        peer.client_kind = "desktop"
        peer.tailscale_ip = tailscale_ip
        save_peers(peers)
        _regenerate_managed_conf(peers)
        return peer


def register_vban_desktop_peer(name: str, tailscale_ip: str) -> Peer:
    """"Connect to Bragi" from the Windows client, whose audio goes over VBAN
    (Voicemeeter's protocol). Same rules as register_desktop_peer: repeatable,
    claims a VBAN peer added from the dashboard under that name (keeping its
    stream names, which Voicemeeter is already set to), and returns a peer
    configured by hand untouched."""
    with _lock:
        peers = load_peers()
        peer = next((p for p in peers if p.name == name), None)
        if peer is not None and (peer.protocol != "vban" or peer.client_kind not in (None, "desktop")):
            raise ValueError(f"'{name}' already belongs to another client")
        if peer is not None and not peer.managed:
            return peer
        if peer is None:
            # The dashboard's defaults: the Pi's mic goes out as BragiMic and
            # this computer's audio comes back as <Name>Audio.
            stream_receive = f"{name.replace('-', '').capitalize()[:11]}Audio"
            if any(p.protocol == "vban" and p.stream_receive == stream_receive for p in peers):
                raise ValueError(f"another VBAN peer already sends a stream named '{stream_receive}'")
            peer = _vban_peer(name, tailscale_ip, "BragiMic", stream_receive)
            peers.append(peer)
        peer.client_kind = "desktop"
        peer.tailscale_ip = tailscale_ip
        save_peers(peers)
        _regenerate_managed_conf(peers)
        return peer


def set_android_streams(name: str, send_enabled: bool, receive_enabled: bool) -> Peer:
    with _lock:
        peers = load_peers()
        peer = next((p for p in peers if p.name == name and p.client_kind == "android"), None)
        if peer is None:
            raise ValueError(f"Android peer '{name}' not found")
        peer.send_enabled = send_enabled and peer.capture_mode != "none"
        peer.receive_enabled = receive_enabled
        save_peers(peers)
        # Only the app changes its streams. Restarting PipeWire peers here
        # would interrupt every managed peer for a control-plane action.
        return peer
