"""Builds the JSON-able view of current state (headsets + peers) shared by
the initial server-rendered page (main.py) and every WebSocket broadcast
(ws.py). Kept separate from both so neither has to import the other.
"""

from __future__ import annotations

from . import microphone
from . import audio_state
from . import headsets as headsets_module
from . import peer_presence
from . import peers as peers_module
from . import pipewire
from . import layout
from . import viz_settings


def resolve_node_id(graph: pipewire.Graph, peer: peers_module.Peer, direction: str) -> int | None:
    """direction: 'outgoing' (sagepi's mic, going to this peer) or
    'incoming' (this peer's audio, arriving at sagepi's headset)."""
    # Roc and VBAN peers alike: the managed modules name both nodes.
    name = peer.outgoing_sink_name if direction == "outgoing" else peer.incoming_source_name
    return pipewire.find_node_id(graph.nodes, name) if name else None


def peer_incoming_node_name(peer: peers_module.Peer) -> str:
    return peer.incoming_source_name


def peer_outgoing_node_name(peer: peers_module.Peer) -> str:
    # The outgoing (mic->peer) direction is just as much a software stream
    # node as incoming is, so it's equally safe to pan (unlike headset
    # directions, see direction_view).
    return peer.outgoing_sink_name


Volumes = dict[int, tuple[float | None, bool]]


def direction_view(node_id: int | None, node_name: str | None = None, volumes: Volumes | None = None) -> dict:
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
    channel skew, so those still come straight from wpctl.

    volumes, when given, is that wpctl reading already taken for this node
    (see build_state) - otherwise it's read here, one call."""
    if node_id is None:
        return {"id": None, "volume": None, "muted": False, "connected": False, "balance": 0.0}
    if volumes is not None and node_id in volumes:
        wpctl_volume, muted = volumes[node_id]
    else:
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


def peer_view(graph: pipewire.Graph, peer: peers_module.Peer, volumes: Volumes | None = None) -> dict:
    from . import peer_control
    out_id = resolve_node_id(graph, peer, "outgoing")
    in_id = resolve_node_id(graph, peer, "incoming")
    client_connected = peer.name in peer_presence.connected_peers if peer.protocol == "roc" else None
    return {
        "name": peer.name,
        "protocol": peer.protocol,
        "tailscale_ip": peer.tailscale_ip,
        "managed": peer.managed,
        "client_kind": peer.client_kind,
        "capture_mode": peer.capture_mode,
        "send_enabled": peer.send_enabled,
        "receive_enabled": peer.receive_enabled,
        "client_status": peer_control.statuses.get(peer.name),
        "outgoing": direction_view(out_id, peer_outgoing_node_name(peer), volumes),
        "incoming": direction_view(in_id, peer_incoming_node_name(peer), volumes),
        # Only meaningful for peers with a Bragi Client tray app (currently
        # Roc peers only - VBAN peers like "sage" have no client yet, see
        # client/README.md). None (not False) for other protocols, so the
        # template can tell "no client mechanism exists for this peer" apart
        # from "client exists but isn't connected right now".
        "client_connected": client_connected,
        # Folded into the dashboard's parked stack (_peers_list.html). A peer
        # with a client (a phone's app, a desktop's tray app) is parked
        # while that client is away: its Roc modules live in peers.conf, so
        # its nodes exist either way. Others park when neither direction
        # has a node.
        "parked": client_connected is False or (out_id is None and in_id is None),
    }


def headset_view(
    hs: headsets_module.Headset, device: pipewire.Device | None, volumes: Volumes | None = None
) -> dict:
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
        "playback": direction_view(hs.playback_node_id, volumes=volumes),
        "capture": direction_view(hs.capture_node_id, volumes=volumes),
    }


def _device_by_card(devices: list[pipewire.Device]) -> dict[str, pipewire.Device]:
    return {d.name[len("alsa_card."):]: d for d in devices if d.name.startswith("alsa_card.")}


def get_headset(graph: pipewire.Graph, key: str) -> headsets_module.Headset | None:
    return next((h for h in headsets_module.list_headsets(graph) if h.key == key), None)


def get_headset_with_device(
    graph: pipewire.Graph, key: str
) -> tuple[headsets_module.Headset, pipewire.Device | None] | None:
    hs = get_headset(graph, key)
    if hs is None:
        return None
    return hs, _device_by_card(graph.devices).get(key)


def get_peer(name: str) -> peers_module.Peer | None:
    return next((p for p in peers_module.load_peers() if p.name == name), None)


def build_state() -> dict:
    graph = pipewire.dump()
    device_by_card = _device_by_card(graph.devices)
    headsets = headsets_module.list_headsets(graph)
    peers = peers_module.load_peers()
    # Every displayed node's volume, read in one concurrent batch before any
    # view is built - one wpctl per direction back to back was ~0.5s of a
    # sagepi page load, growing by two calls with every peer.
    node_ids = {i for h in headsets for i in (h.playback_node_id, h.capture_node_id)}
    node_ids |= {resolve_node_id(graph, p, d) for p in peers for d in ("outgoing", "incoming")}
    node_ids.discard(None)
    volumes = pipewire.get_volume_mute_many(node_ids)
    default_order = [f"headset:{h.key}" for h in headsets] + [f"peer:{p.name}" for p in peers]
    return {
        "headsets": [headset_view(h, device_by_card.get(h.key), volumes) for h in headsets],
        "peers": [peer_view(graph, p, volumes) for p in peers],
        "order_of": layout.positions(default_order),
        "viz_settings": {"enabled": viz_settings.get_enabled()},
        "microphone": microphone.view(),
    }


def find_controls_for_nodes(graph: pipewire.Graph, node_ids: set[int]) -> set[tuple[str, str, str]]:
    """Maps PipeWire node ids back to the (target, key, direction) controls
    that display them, dropping ids the dashboard shows nothing for (ports,
    links, video capture, the MIDI bridge...).

    Takes the whole id set at once, and resolves it against one graph,
    because the caller is the pw-mon watcher and its events arrive in
    bursts: a single headset profile flip emits 147 changed events across
    72 distinct ids. Resolving those one at a time, each against its own
    fresh pw-dump, is what made enable/disable take ~9.7s. Note how few
    controls a burst that size actually maps to - usually one or two - so
    the returned set also collapses the redundant broadcasts."""
    controls: set[tuple[str, str, str]] = set()
    for hs in headsets_module.list_headsets(graph):
        if hs.playback_node_id in node_ids:
            controls.add(("headset", hs.key, "playback"))
        if hs.capture_node_id in node_ids:
            controls.add(("headset", hs.key, "capture"))
    for peer in peers_module.load_peers():
        for direction in ("outgoing", "incoming"):
            if resolve_node_id(graph, peer, direction) in node_ids:
                controls.add(("peer", peer.name, direction))
    return controls


def headset_control_view(graph: pipewire.Graph, key: str, direction: str) -> dict | None:
    hs = get_headset(graph, key)
    if hs is None:
        return None
    node_id = hs.playback_node_id if direction == "playback" else hs.capture_node_id
    return direction_view(node_id)


def peer_control_view(graph: pipewire.Graph, key: str, direction: str) -> dict | None:
    peer = get_peer(key)
    if peer is None:
        return None
    node_id = resolve_node_id(graph, peer, direction)
    if direction == "incoming":
        node_name = peer_incoming_node_name(peer)
    elif direction == "outgoing":
        node_name = peer_outgoing_node_name(peer)
    else:
        node_name = None
    return direction_view(node_id, node_name)
