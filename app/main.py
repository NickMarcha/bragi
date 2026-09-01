from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import knob_watcher
from . import level_meter
from . import peer_presence
from . import peers as peers_module
from . import pipewire
from . import views
from . import watcher
from . import ws


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(watcher.watch(ws.on_node_changed)),
        asyncio.create_task(knob_watcher.watch(ws.broadcast_headset_volume_change)),
        asyncio.create_task(level_meter.supervise()),
    ]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Bragi", lifespan=lifespan)

_app_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_app_dir / "static")), name="static")
templates = Jinja2Templates(directory=str(_app_dir / "templates"))


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    state = views.build_state()
    return templates.TemplateResponse(request, "index.html", state)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await ws.websocket_endpoint(websocket)


@app.websocket("/ws/peer/{name}")
async def peer_presence_ws(websocket: WebSocket, name: str) -> None:
    """A Bragi Client tray app holds this connection open for as long as its
    Roc link is enabled - see peer_presence.py's docstring for why this
    (not PipeWire node presence) is the real "is this peer reachable"
    signal. Reuses ws.manager's existing broadcast_nowait to notify open
    dashboard tabs, but is otherwise fully independent of ws.py's fader
    control-plane state."""
    await websocket.accept()
    peer_presence.connected_peers.add(name)
    ws.manager.broadcast_nowait({"type": "peer_presence", "name": name, "connected": True})
    try:
        while True:
            await websocket.receive_text()  # never sends anything meaningful - just holds the socket open
    except WebSocketDisconnect:
        pass
    finally:
        peer_presence.connected_peers.discard(name)
        ws.manager.broadcast_nowait({"type": "peer_presence", "name": name, "connected": False})


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
    views_list = [views.peer_view(nodes, p) for p in peers_module.load_peers()]
    return templates.TemplateResponse(request, "_peers_list.html", {"peers": views_list})


@app.post("/peers/{name}/delete", response_class=HTMLResponse)
def delete_peer(request: Request, name: str):
    try:
        peers_module.remove_peer(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except peers_module.ConfigWriteError as exc:
        raise HTTPException(502, str(exc)) from exc
    state = views.build_state()
    return templates.TemplateResponse(request, "_peers_list.html", {"peers": state["peers"]})
