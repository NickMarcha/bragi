from __future__ import annotations

import asyncio
import contextlib
import logging
import ipaddress
from typing import Literal
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from . import knob_watcher
from . import level_meter
from . import peer_control
from . import peers as peers_module
from . import views
from . import watcher
from . import ws

logger = logging.getLogger("bragi")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await asyncio.to_thread(peers_module.sync_managed_conf)
    except peers_module.ConfigWriteError:
        logger.exception("managed peers config not written")
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
    await peer_control.endpoint(websocket, name)


class AndroidRegistration(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    tailscale_ip: str
    capture_mode: Literal["microphone", "device_audio"] = "microphone"

    @field_validator("tailscale_ip")
    @classmethod
    def tailnet_address(cls, value: str) -> str:
        address = ipaddress.ip_address(value)
        if address not in ipaddress.ip_network("100.64.0.0/10"):
            raise ValueError("Use the phone's Tailscale IPv4 address")
        return str(address)


class StreamSettings(BaseModel):
    send_enabled: bool = Field(strict=True)
    receive_enabled: bool = Field(strict=True)


@app.post("/api/peers/register")
async def register_android_peer(settings: AndroidRegistration):
    try:
        peer = await asyncio.to_thread(peers_module.register_android_peer, settings.name,
                                       settings.tailscale_ip, settings.capture_mode)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except peers_module.ConfigWriteError as exc:
        raise HTTPException(502, str(exc)) from exc
    ws.manager.broadcast_nowait({'type': 'peer_registry'})
    return peer_control.config(peer)


@app.post("/api/peers/{name}/streams")
async def set_peer_streams(name: str, settings: StreamSettings):
    try:
        peer = await asyncio.to_thread(peers_module.set_android_streams, name,
                                       settings.send_enabled, settings.receive_enabled)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return peer_control.publish_streams(peer)


@app.post("/peers", response_class=HTMLResponse)
def add_peer(request: Request, name: str = Form(...), tailscale_ip: str = Form(...)):
    name = name.strip().lower().replace(" ", "-")
    try:
        peers_module.add_roc_peer(name, tailscale_ip.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except peers_module.ConfigWriteError as exc:
        raise HTTPException(502, str(exc)) from exc
    state = views.build_state()
    return templates.TemplateResponse(request, "_peers_list.html", {"peers": state["peers"]})


@app.get("/peers", response_class=HTMLResponse)
def peer_cards(request: Request):
    state = views.build_state()
    return templates.TemplateResponse(request, "_peers_list.html", {"peers": state["peers"]})


@app.post("/peers/{name}/delete", response_class=HTMLResponse)
async def delete_peer(request: Request, name: str):
    try:
        await asyncio.to_thread(peers_module.remove_peer, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except peers_module.ConfigWriteError as exc:
        raise HTTPException(502, str(exc)) from exc
    peer_control.remove(name)
    state = await asyncio.to_thread(views.build_state)
    return templates.TemplateResponse(request, "_peers_list.html", {"peers": state["peers"]})
