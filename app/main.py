from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import audio_state
from . import headsets as headsets_module
from . import peers as peers_module
from . import pipewire

app = FastAPI(title="Bragi")

_app_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_app_dir / "static")), name="static")
templates = Jinja2Templates(directory=str(_app_dir / "templates"))


def _resolve_node_id(nodes: list[pipewire.Node], peer: peers_module.Peer, direction: str) -> int | None:
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


def _direction_view(node_id: int | None, node_name: str | None = None) -> dict:
    """node_name, when given, marks this direction as stereo/pannable -
    only those directions get a balance slider (see pipewire.set_channel_volumes'
    mono-node caveat: mic-direction nodes are typically mono, so they're
    called with node_name=None and stay volume+mute only)."""
    if node_id is None:
        return {"id": None, "volume": None, "muted": False, "connected": False, "balance": 0.0}
    volume, muted = pipewire.get_volume_mute(node_id)
    view = {"id": node_id, "volume": volume, "muted": muted, "connected": volume is not None}
    view["balance"] = audio_state.get_balance(node_name) if node_name else 0.0
    return view


def _peer_incoming_node_name(peer: peers_module.Peer) -> str:
    # VBAN's incoming node is always literally named "vban" (see
    # _resolve_node_id) - balance state would collide across multiple VBAN
    # peers, same limitation the README already documents for VBAN peers.
    return peer.incoming_source_name if peer.protocol == "roc" else "vban-incoming"


def _peer_view(nodes: list[pipewire.Node], peer: peers_module.Peer) -> dict:
    out_id = _resolve_node_id(nodes, peer, "outgoing")
    in_id = _resolve_node_id(nodes, peer, "incoming")
    return {
        "peer": peer,
        "outgoing": _direction_view(out_id),
        "incoming": _direction_view(in_id, _peer_incoming_node_name(peer)),
    }


def _headset_view(hs: headsets_module.Headset) -> dict:
    return {
        "headset": hs,
        "playback": _direction_view(hs.playback_node_id, hs.playback_node_name),
        "capture": _direction_view(hs.capture_node_id),
    }


def _get_headset(key: str) -> headsets_module.Headset:
    hs = next((h for h in headsets_module.list_headsets(pipewire.list_nodes()) if h.key == key), None)
    if hs is None:
        raise HTTPException(404, "unknown headset")
    return hs


def _get_peer(name: str) -> peers_module.Peer:
    peer = next((p for p in peers_module.load_peers() if p.name == name), None)
    if peer is None:
        raise HTTPException(404, "unknown peer")
    return peer


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    nodes = pipewire.list_nodes()
    headset_views = [_headset_view(h) for h in headsets_module.list_headsets(nodes)]
    peer_views = [_peer_view(nodes, p) for p in peers_module.load_peers()]
    return templates.TemplateResponse(request, "index.html", {"headsets": headset_views, "peers": peer_views})


@app.get("/peers/{name}/card", response_class=HTMLResponse)
def peer_card(request: Request, name: str):
    peer = _get_peer(name)
    nodes = pipewire.list_nodes()
    return templates.TemplateResponse(request, "_peer_card.html", {"view": _peer_view(nodes, peer)})


@app.post("/peers/{name}/volume/{direction}", response_class=HTMLResponse)
def set_volume(request: Request, name: str, direction: str, value: float = Form(...)):
    peer = _get_peer(name)
    nodes = pipewire.list_nodes()
    node_id = _resolve_node_id(nodes, peer, direction)
    if node_id is not None:
        if direction == "incoming":
            balance = audio_state.get_balance(_peer_incoming_node_name(peer))
            pipewire.set_channel_volumes(node_id, value, balance)
        else:
            pipewire.set_volume(node_id, value)
    return templates.TemplateResponse(request, "_peer_card.html", {"view": _peer_view(nodes, peer)})


@app.post("/peers/{name}/balance/incoming", response_class=HTMLResponse)
def set_peer_balance(request: Request, name: str, value: float = Form(...)):
    peer = _get_peer(name)
    nodes = pipewire.list_nodes()
    node_id = _resolve_node_id(nodes, peer, "incoming")
    if node_id is not None:
        volume, _muted = pipewire.get_volume_mute(node_id)
        pipewire.set_channel_volumes(node_id, volume if volume is not None else 1.0, value)
        audio_state.set_balance(_peer_incoming_node_name(peer), value)
    return templates.TemplateResponse(request, "_peer_card.html", {"view": _peer_view(nodes, peer)})


@app.post("/peers/{name}/mute/{direction}", response_class=HTMLResponse)
def toggle_mute(request: Request, name: str, direction: str):
    peer = _get_peer(name)
    nodes = pipewire.list_nodes()
    node_id = _resolve_node_id(nodes, peer, direction)
    if node_id is not None:
        _, currently_muted = pipewire.get_volume_mute(node_id)
        pipewire.set_mute(node_id, not currently_muted)
    return templates.TemplateResponse(request, "_peer_card.html", {"view": _peer_view(nodes, peer)})


@app.get("/headsets/{key}/card", response_class=HTMLResponse)
def headset_card(request: Request, key: str):
    hs = _get_headset(key)
    return templates.TemplateResponse(request, "_headset_card.html", {"view": _headset_view(hs)})


@app.post("/headsets/{key}/volume/{direction}", response_class=HTMLResponse)
def set_headset_volume(request: Request, key: str, direction: str, value: float = Form(...)):
    hs = _get_headset(key)
    if direction == "playback" and hs.playback_node_id is not None:
        balance = audio_state.get_balance(hs.playback_node_name)
        pipewire.set_channel_volumes(hs.playback_node_id, value, balance)
    elif direction == "capture" and hs.capture_node_id is not None:
        pipewire.set_volume(hs.capture_node_id, value)
    return templates.TemplateResponse(request, "_headset_card.html", {"view": _headset_view(hs)})


@app.post("/headsets/{key}/mute/{direction}", response_class=HTMLResponse)
def toggle_headset_mute(request: Request, key: str, direction: str):
    hs = _get_headset(key)
    node_id = hs.playback_node_id if direction == "playback" else hs.capture_node_id
    if node_id is not None:
        _, currently_muted = pipewire.get_volume_mute(node_id)
        pipewire.set_mute(node_id, not currently_muted)
    return templates.TemplateResponse(request, "_headset_card.html", {"view": _headset_view(hs)})


@app.post("/headsets/{key}/balance/playback", response_class=HTMLResponse)
def set_headset_balance(request: Request, key: str, value: float = Form(...)):
    hs = _get_headset(key)
    if hs.playback_node_id is not None:
        volume, _muted = pipewire.get_volume_mute(hs.playback_node_id)
        pipewire.set_channel_volumes(hs.playback_node_id, volume if volume is not None else 1.0, value)
        audio_state.set_balance(hs.playback_node_name, value)
    return templates.TemplateResponse(request, "_headset_card.html", {"view": _headset_view(hs)})


@app.post("/peers", response_class=HTMLResponse)
def add_peer(request: Request, name: str = Form(...), tailscale_ip: str = Form(...)):
    name = name.strip().lower().replace(" ", "-")
    try:
        peers_module.add_roc_peer(name, tailscale_ip.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except peers_module.ConfigWriteError as exc:
        raise HTTPException(502, str(exc)) from exc
    nodes = pipewire.list_nodes()
    views = [_peer_view(nodes, p) for p in peers_module.load_peers()]
    return templates.TemplateResponse(request, "_peers_list.html", {"peers": views})


@app.post("/peers/{name}/delete", response_class=HTMLResponse)
def delete_peer(request: Request, name: str):
    try:
        peers_module.remove_peer(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except peers_module.ConfigWriteError as exc:
        raise HTTPException(502, str(exc)) from exc
    nodes = pipewire.list_nodes()
    views = [_peer_view(nodes, p) for p in peers_module.load_peers()]
    return templates.TemplateResponse(request, "_peers_list.html", {"peers": views})
