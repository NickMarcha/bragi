"""Builds the JSON-able view of current state (headsets + peers) shared by
the initial server-rendered page (main.py) and every WebSocket broadcast
(ws.py). Kept separate from both so neither has to import the other.
"""

from __future__ import annotations

from . import audio_state
from . import headsets as headsets_module
from . import peer_presence
from . import peers as peers_module
from . import pipewire
from . import viz_settings


def resolve_node_id(nodes: list[pipewire.Node], peer: peers_module.Peer, direction: str) -> int | None:
    """direction: 'outgoing' (sagepi's mic, going to this peer) or
    'incoming' (this peer's audio, arriving at sagepi's headset)."""
    if peer.protocol == "roc":
        name = peer.outgoing_sink_name if direction == "outgoing" else peer.incoming_source_name
        return pipewire.find_node_id(nodes, name) if name else None
    if peer.protocol == "vban":
        # VBAN clients are always named literally "vban" - direction is the
        # only thing that distinguishes them, which only works cleanly with
        # a single VBAN peer. See README's "Known Limitations".
        wanted_class = "Stream/Input/Audio" if direction == "outgoing" else "Stream/Output/Audio"
        for node in nodes:
            if node.name == "vban" and node.media_class == wanted_class:
                return node.id
    return None


def peer_incoming_node_name(peer: peers_module.Peer) -> str:
    # VBAN's incoming node is always literally named "vban" (see
    # resolve_node_id) - balance state would collide across multiple VBAN
    # peers, same limitation the README already documents for VBAN peers.
    return peer.incoming_source_name if peer.protocol == "roc" else "vban-incoming"


def peer_outgoing_node_name(peer: peers_module.Peer) -> str:
    # Mirrors peer_incoming_node_name - the outgoing (mic->peer) direction is
    # just as much a software Roc/VBAN stream node as incoming is, so it's
    # equally safe to pan (unlike headset directions, see direction_view).
    return peer.outgoing_sink_name if peer.protocol == "roc" else "vban-outgoing"


def direction_view(node_id: int | None, node_name: str | None = None) -> dict:
    """node_name, when given, marks this direction as stereo/pannable -
    only those directions get a balance slider. Headset directions are
    real ALSA hardware nodes and never pass node_name: WirePlumber's
    alsa-monitor treats the hardware mixer as authoritative and reverts any
    software channel-volume write, so balance only actually holds on
    software nodes (Roc/VBAN peer streams).

    For a pannable direction, the displayed *volume* comes from
    audio_state's stored value, never from wpctl - wpctl only ever reports
    the FL channel, which is already skewed once balance != 0. Showing (or
    recomputing from) that skewed reading is what caused volume to ratchet
    down on every balance adjustment. muted/connected are unaffected by
    channel skew, so those still come straight from wpctl."""
    if node_id is None:
        return {"id": None, "volume": None, "muted": False, "connected": False, "balance": 0.0}
    wpctl_volume, muted = pipewire.get_volume_mute(node_id)
    view = {"id": node_id, "muted": muted, "connected": wpctl_volume is not None}
    if node_name:
        stored_volume, stored_balance = audio_state.get_state(node_name)
        view["volume"] = stored_volume
        view["balance"] = stored_balance
    else:
        view["volume"] = wpctl_volume
        view["balance"] = 0.0
    return view


def peer_view(nodes: list[pipewire.Node], peer: peers_module.Peer) -> dict:
    out_id = resolve_node_id(nodes, peer, "outgoing")
    in_id = resolve_node_id(nodes, peer, "incoming")
    return {
        "name": peer.name,
        "protocol": peer.protocol,
        "tailscale_ip": peer.tailscale_ip,
        "managed": peer.managed,
        "outgoing": direction_view(out_id, peer_outgoing_node_name(peer)),
        "incoming": direction_view(in_id, peer_incoming_node_name(peer)),
        # Only meaningful for peers with a Bragi Client tray app (currently
        # Roc peers only - VBAN's "sage" has no client yet, see
        # client/README.md). None (not False) for other protocols, so the
        # template can tell "no client mechanism exists for this peer" apart
        # from "client exists but isn't connected right now".
        "client_connected": peer.name in peer_presence.connected_peers if peer.protocol == "roc" else None,
    }


def headset_view(hs: headsets_module.Headset, device: pipewire.Device | None) -> dict:
    enabled = True
    device_id = None
    if device is not None:
        device_id = device.id
        if device.off_profile_index is not None:
            enabled = device.active_profile_index != device.off_profile_index
    return {
        "key": hs.key,
        "label": hs.label,
        "enabled": enabled,
        "device_id": device_id,
        "playback": direction_view(hs.playback_node_id),
        "capture": direction_view(hs.capture_node_id),
    }


def _device_by_card(devices: list[pipewire.Device]) -> dict[str, pipewire.Device]:
    return {d.name[len("alsa_card."):]: d for d in devices if d.name.startswith("alsa_card.")}


def get_headset(key: str) -> headsets_module.Headset | None:
    """Node-based only (cheap, no device scan) - returns None for a
    currently-disabled headset, which is fine for the throttled volume/mute
    hot path this is used by (a no-op on a disabled headset is correct).
    Use get_headset_with_device for anything that must also work when
    disabled (re-enabling, the dashboard listing)."""
    return next((h for h in headsets_module.list_headsets(pipewire.list_nodes()) if h.key == key), None)


def get_headset_with_device(key: str) -> tuple[headsets_module.Headset, pipewire.Device | None] | None:
    nodes = pipewire.list_nodes()
    devices = pipewire.list_devices()
    for hs in headsets_module.list_headsets(nodes, devices):
        if hs.key == key:
            return hs, _device_by_card(devices).get(key)
    return None


def get_peer(name: str) -> peers_module.Peer | None:
    return next((p for p in peers_module.load_peers() if p.name == name), None)


def build_state() -> dict:
    nodes = pipewire.list_nodes()
    devices = pipewire.list_devices()
    device_by_card = _device_by_card(devices)
    return {
        "headsets": [
            headset_view(h, device_by_card.get(h.key)) for h in headsets_module.list_headsets(nodes, devices)
        ],
        "peers": [peer_view(nodes, p) for p in peers_module.load_peers()],
        "viz_settings": {"enabled": viz_settings.get_enabled()},
    }


def find_control_for_node(node_id: int) -> tuple[str, str, str] | None:
    """Maps a PipeWire node id back to the (target, key, direction) control
    that displays it, or None if it's not something the dashboard shows
    (video capture, MIDI bridge, etc.). Used by the watcher so a hardware
    knob turn triggers a *targeted* refresh of just that one control,
    not a full-graph rebuild - see headset_control_view/peer_control_view,
    the whole reason a full build_state() per change was too slow to feel
    live while dragging (confirmed live: ~625ms for the full graph vs
    ~100-150ms for one control, on sagepi's Pi 4)."""
    nodes = pipewire.list_nodes()
    for hs in headsets_module.list_headsets(nodes):
        if hs.playback_node_id == node_id:
            return ("headset", hs.key, "playback")
        if hs.capture_node_id == node_id:
            return ("headset", hs.key, "capture")
    for peer in peers_module.load_peers():
        for direction in ("outgoing", "incoming"):
            if resolve_node_id(nodes, peer, direction) == node_id:
                return ("peer", peer.name, direction)
    return None


def headset_control_view(key: str, direction: str) -> dict | None:
    hs = get_headset(key)
    if hs is None:
        return None
    node_id = hs.playback_node_id if direction == "playback" else hs.capture_node_id
    return direction_view(node_id)


def peer_control_view(key: str, direction: str) -> dict | None:
    peer = get_peer(key)
    if peer is None:
        return None
    nodes = pipewire.list_nodes()
    node_id = resolve_node_id(nodes, peer, direction)
    if direction == "incoming":
        node_name = peer_incoming_node_name(peer)
    elif direction == "outgoing":
        node_name = peer_outgoing_node_name(peer)
    else:
        node_name = None
    return direction_view(node_id, node_name)
