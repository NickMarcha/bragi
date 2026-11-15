from __future__ import annotations

import asyncio
from dataclasses import asdict
import contextlib
import logging
import ipaddress
import os
from typing import Literal
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from . import access
from . import api
from . import audio_reset
from . import auth
from . import bluetooth
from . import microphone
from . import mic_monitor
from . import eq_presets
from . import gamepad
from . import headset_hid
from . import knob_watcher
from . import kvm
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
        asyncio.create_task(headset_hid.supervise(ws.broadcast_headset_controls, ws.on_macro_button)),
        asyncio.create_task(level_meter.supervise()),
        asyncio.create_task(microphone.supervise()),
        asyncio.create_task(mic_monitor.supervise()),
        asyncio.create_task(gamepad.supervise(peer_control.send, peer_control.broadcast_desktop)),
        asyncio.create_task(kvm.supervise(_kvm_changed)),
    ]
    bluetooth.start(_bluetooth_changed)
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _kvm_changed(message: dict) -> None:
    """The KVM's computers or active one changed: dashboards and tray apps."""
    peer_control.broadcast_desktop(message)


_bluetooth_pending = False


def _bluetooth_changed() -> None:
    """BlueZ reports every signal-strength change while scanning; tell the
    dashboard at most twice a second."""
    global _bluetooth_pending
    if _bluetooth_pending:
        return
    _bluetooth_pending = True

    def fire() -> None:
        global _bluetooth_pending
        _bluetooth_pending = False
        ws.manager.broadcast_nowait({'type': 'bluetooth'})

    asyncio.get_running_loop().call_later(0.5, fire)


app = FastAPI(title="Bragi", lifespan=lifespan)
app.add_middleware(access.AccessMiddleware)
app.include_router(access.router)
app.include_router(api.router)

_app_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_app_dir / "static")), name="static")
templates = Jinja2Templates(directory=str(_app_dir / "templates"))
# The EQ bands and auto-off choices, for _headset_controls.html.
templates.env.globals["hid"] = headset_hid
templates.env.globals["eq_preset_store"] = eq_presets


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    state = views.build_state()
    return templates.TemplateResponse(request, "index.html", {**state, "password_set": auth.password_set(),
                                                              "kvm": kvm.read_view()})


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


class DesktopRegistration(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    tailscale_ip: str

    @field_validator("tailscale_ip")
    @classmethod
    def tailnet_address(cls, value: str) -> str:
        address = ipaddress.ip_address(value)
        # A local dev server (dev/server.py) reaches the emulator through adb
        # UDP redirects on the host's loopback, never over Tailscale.
        if os.environ.get("BRAGI_LOCAL_DEV") == "1" and address == ipaddress.ip_address("127.0.0.1"):
            return str(address)
        if address not in ipaddress.ip_network("100.64.0.0/10"):
            raise ValueError("Use the device's Tailscale IPv4 address")
        return str(address)


class StreamSettings(BaseModel):
    send_enabled: bool = Field(strict=True)
    receive_enabled: bool = Field(strict=True)


class DesktopLinkRegistration(DesktopRegistration):
    # The Linux client links over Roc, the Windows one over VBAN.
    protocol: Literal["roc", "vban"] = "roc"


class AndroidRegistration(DesktopRegistration):
    capture_mode: Literal["microphone", "device_audio", "none"] = "microphone"


@app.post("/api/peers/register-desktop")
async def register_desktop_peer(settings: DesktopLinkRegistration):
    """The Bragi Client's setup window. Returns the ports its Roc link unit
    needs, or for a VBAN client (Windows) the port and stream names."""
    register = (peers_module.register_vban_desktop_peer if settings.protocol == "vban"
                else peers_module.register_desktop_peer)
    try:
        peer = await asyncio.to_thread(register, settings.name, settings.tailscale_ip)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except peers_module.ConfigWriteError as exc:
        raise HTTPException(502, str(exc)) from exc
    ws.manager.broadcast_nowait({'type': 'peer_registry'})
    if peer.protocol == "vban":
        return {'name': peer.name, 'vban': {'port': peer.vban_port, 'stream_send': peer.stream_send,
                                            'stream_receive': peer.stream_receive}}
    return {'name': peer.name, 'ports': asdict(peer.ports)}


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


@app.get("/audio/microphone", response_class=HTMLResponse)
def microphone_controls(request: Request):
    return templates.TemplateResponse(request, "_microphone.html", {'microphone': microphone.view()})


@app.post("/audio/microphone", response_class=HTMLResponse)
def select_microphone(request: Request, source: str = Form(...)):
    try:
        microphone.select(source)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except peers_module.pipewire.PipewireError as exc:
        raise HTTPException(502, str(exc)) from exc
    ws.manager.broadcast_nowait({'type': 'microphone'})
    return microphone_controls(request)


async def _bluetooth_view(request: Request):
    """The Bluetooth dialog: paired devices, pairing new ones, and which
    computer the controllers play on (app/bluetooth.py, app/gamepad.py)."""
    if bluetooth.manager is None:
        view = {"powered": False, "scanning": False, "paired": [], "found": [], "pairing": None,
                "error": "Bluetooth isn't running in this Bragi."}
    else:
        view = await bluetooth.manager.refresh()
    computers = [p.name for p in peers_module.load_peers() if p.client_kind != 'android']
    status = gamepad.hub.status() if gamepad.hub is not None else {"target": gamepad.target(), "controllers": []}
    return templates.TemplateResponse(request, "_bluetooth.html", {
        "bt": view, "computers": computers, "target": status["target"],
        "playing": {c["id"] for c in status["controllers"]}})


def _bluetooth() -> bluetooth.Manager:
    if bluetooth.manager is None:
        raise HTTPException(503, "Bluetooth isn't running in this Bragi.")
    return bluetooth.manager


_ADDRESS = r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$"


def _kvm_bar(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "_kvm.html", {"kvm": kvm.read_view(), "kvm_error": error})


@app.get("/kvm", response_class=HTMLResponse)
def kvm_bar(request: Request):
    return _kvm_bar(request)


@app.post("/kvm/switch", response_class=HTMLResponse)
async def kvm_switch(request: Request, computer: str = Form(..., max_length=64)):
    try:
        await asyncio.to_thread(kvm.switch, computer)
    except kvm.KvmError as exc:
        return _kvm_bar(request, str(exc))
    return _kvm_bar(request)


@app.get("/bluetooth", response_class=HTMLResponse)
async def bluetooth_page(request: Request):
    return await _bluetooth_view(request)


@app.post("/bluetooth/scan", response_class=HTMLResponse)
async def bluetooth_scan(request: Request):
    await _bluetooth().scan()
    return await _bluetooth_view(request)


@app.post("/bluetooth/stop", response_class=HTMLResponse)
async def bluetooth_stop(request: Request):
    await _bluetooth().stop_scan()
    return await _bluetooth_view(request)


@app.post("/bluetooth/pair", response_class=HTMLResponse)
async def bluetooth_pair(request: Request, address: str = Form(..., pattern=_ADDRESS)):
    # Pairing takes several seconds; the dialog follows it through broadcasts.
    asyncio.create_task(_bluetooth().pair(address))
    await asyncio.sleep(0)
    return await _bluetooth_view(request)


@app.post("/bluetooth/forget", response_class=HTMLResponse)
async def bluetooth_forget(request: Request, address: str = Form(..., pattern=_ADDRESS)):
    await _bluetooth().forget(address)
    return await _bluetooth_view(request)


@app.post("/bluetooth/target", response_class=HTMLResponse)
async def bluetooth_target(request: Request, target: str = Form("")):
    computers = {p.name for p in peers_module.load_peers() if p.client_kind != 'android'}
    if target and target not in computers:
        raise HTTPException(422, f"No computer named {target!r}")
    if gamepad.hub is None:
        raise HTTPException(503, "Controllers aren't running in this Bragi.")
    gamepad.hub.set_target(target or None)
    return await _bluetooth_view(request)


@app.get("/audio/reset", response_class=HTMLResponse)
def audio_reset_control(request: Request):
    return templates.TemplateResponse(request, "_audio_reset.html", {'audio_reset': audio_reset.view()})


@app.post("/audio/reset", response_class=HTMLResponse)
def reset_audio(request: Request):
    try:
        audio_reset.request()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(502, f"could not write the reset request ({exc})") from exc
    return audio_reset_control(request)


@app.post("/peers", response_class=HTMLResponse)
def add_peer(request: Request, name: str = Form(...), tailscale_ip: str = Form(...),
             protocol: Literal["roc", "vban"] = Form("roc"), stream_send: str = Form(""),
             stream_receive: str = Form("")):
    name = name.strip().lower().replace(" ", "-")
    try:
        if protocol == "vban":
            # Defaults: the Pi's mic goes out as BragiMic and the peer's
            # audio comes back as <Name>Audio. Set Voicemeeter to match.
            peers_module.add_vban_peer(name, tailscale_ip.strip(), stream_send.strip() or "BragiMic",
                                       stream_receive.strip() or f"{name.replace('-', '').capitalize()[:11]}Audio")
        else:
            peers_module.add_roc_peer(name, tailscale_ip.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except peers_module.ConfigWriteError as exc:
        raise HTTPException(502, str(exc)) from exc
    state = views.build_state()
    return templates.TemplateResponse(request, "_peers_list.html", {"peers": state["peers"], "headsets": state["headsets"], "order_of": state["order_of"]})


@app.get("/peers", response_class=HTMLResponse)
def peer_cards(request: Request):
    state = views.build_state()
    return templates.TemplateResponse(request, "_peers_list.html", {"peers": state["peers"], "headsets": state["headsets"], "order_of": state["order_of"]})


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
    return templates.TemplateResponse(request, "_peers_list.html", {"peers": state["peers"], "headsets": state["headsets"], "order_of": state["order_of"]})
