from __future__ import annotations

import asyncio
from dataclasses import asdict
import contextlib
import logging
import ipaddress
import os
import time
from typing import Literal
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from . import access
from . import api
from . import audio_reset
from . import downloads
from . import monitors
from . import pairing
from . import auth
from . import bluetooth
from . import microphone
from . import mic_monitor
from . import eq_presets
from . import gamepad
from . import hardware
from . import headset_hid
from . import knob_watcher
from . import kvm
from . import kvm_config
from . import kvm_follow
from . import modules
from . import updates
from . import setup_checks
from . import hub_info
from . import level_meter
from . import peer_control
from . import key_bindings
from . import hotkey_actions
from . import actions
from . import hidpp
from . import peers as peers_module
from . import views
from . import watcher
from . import ws

logger = logging.getLogger("bragi")


async def _headset_changed(serial: str) -> None:
    """A headset's HID state changed: the dashboards hear about it, and in
    "follow the headset's mute" mode its mute picks the desktop mic."""
    await ws.broadcast_headset_controls(serial)
    await microphone.on_headset_change()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await asyncio.to_thread(peers_module.sync_managed_conf)
    except peers_module.ConfigWriteError:
        logger.exception("managed peers config not written")
    monitors.broadcast = ws.manager.broadcast_nowait
    microphone.broadcast = ws.manager.broadcast_nowait
    kvm_follow.start(asyncio.get_running_loop(), ws.manager.broadcast_nowait)
    monitors.on_change = kvm_follow.changed
    tasks = [
        asyncio.create_task(watcher.watch(ws.on_node_changed)),
        asyncio.create_task(knob_watcher.watch(ws.broadcast_headset_volume_change)),
        asyncio.create_task(headset_hid.supervise(_headset_changed, ws.on_macro_button)),
        asyncio.create_task(level_meter.supervise()),
        asyncio.create_task(microphone.supervise()),
        asyncio.create_task(hub_info.refresh()),
        asyncio.create_task(mic_monitor.supervise()),
        asyncio.create_task(gamepad.supervise(peer_control.send, peer_control.broadcast_desktop,
                                              ws.manager.broadcast_levels_nowait)),
        asyncio.create_task(kvm.supervise(_kvm_changed)),
        asyncio.create_task(hardware.watch_batteries()),
        asyncio.create_task(hidpp.supervise()),
        asyncio.create_task(kvm.listen_actions(hotkey_actions.run)),
        asyncio.create_task(updates.watch(ws.manager.broadcast_nowait)),
        asyncio.create_task(downloads.watch()),
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
templates.env.filters["datetimeformat"] = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    state = views.build_state()
    return templates.TemplateResponse(request, "index.html", {**state, "password_set": auth.password_set(),
                                                              "kvm": kvm.read_view(), "modules": modules.enabled(),
                                                              "updates": updates.status(),
                                                              "downloads": downloads.links()})


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


class PairRequest(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=40)


@app.post("/api/peers/pair")
def pair_with_code(body: PairRequest):
    """A tray app redeems a code from "Connect a device" for its own API key
    (app/pairing.py)."""
    try:
        return {"api_key": pairing.redeem(body.code, body.name)}
    except pairing.PairingError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/pair", response_class=HTMLResponse)
def pair_new(request: Request, origin: str = Form(...)):
    """"Connect a device": a one-time code as a QR code and a link."""
    try:
        server = pairing.server_url(origin)
    except pairing.PairingError as exc:
        return templates.TemplateResponse(request, "_pair.html", {"error": str(exc)})
    code = pairing.new_code()
    link = pairing.link(server, code)
    return templates.TemplateResponse(request, "_pair.html", {
        "code": code, "link": link, "qr": pairing.qr_svg(link)})


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


@app.post("/audio/microphone/mute", response_class=HTMLResponse)
def mute_microphones(request: Request, muted: str = Form(...)):
    """Mutes or unmutes every mic the computers hear (microphone.py)."""
    try:
        microphone.set_muted(muted == "1")
    except peers_module.pipewire.PipewireError as exc:
        raise HTTPException(502, str(exc)) from exc
    ws.manager.broadcast_nowait({'type': 'microphone'})
    return microphone_controls(request)


async def _bluetooth_view(request: Request, extras_error: str | None = None):
    """The Bluetooth dialog: paired devices, pairing new ones, which
    computer the controllers play on and what their extra buttons do
    (app/bluetooth.py, app/gamepad.py)."""
    if bluetooth.manager is None:
        view = {"powered": False, "scanning": False, "paired": [], "found": [], "pairing": None,
                "error": "Bluetooth isn't running in this Bragi."}
    else:
        view = await bluetooth.manager.refresh()
    computers = [p.name for p in peers_module.load_peers() if p.client_kind != 'android']
    status = gamepad.hub.status() if gamepad.hub is not None else {"target": gamepad.target(), "controllers": []}
    return templates.TemplateResponse(request, "_bluetooth.html", {
        "bt": view, "computers": computers, "target": status["target"],
        "controllers": status["controllers"], "playing": {c["id"] for c in status["controllers"]},
        "extras": gamepad.hub.extras() if gamepad.hub is not None else gamepad.extras(),
        "extra_labels": gamepad.EXTRA_LABELS, "extras_error": extras_error})


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


@app.post("/kvm/identify")
async def kvm_identify():
    """Shows each computer's name on its own screens (via its tray app)."""
    try:
        computers = (await asyncio.to_thread(kvm_config.read))["computers"]
    except kvm_config.ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    return peer_control.identify(computers)


# --- KVM settings (app/kvm_config.py), for the dashboard's KVM setup ---

def _kvm_config_call(fn, *args):
    try:
        return {"ok": True, "note": fn(*args), "config": kvm_config.read()}
    except kvm_config.ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/kvm/monitors")
async def kvm_monitors():
    """Each KVM computer's monitors, as its tray app reported them (app/monitors.py)."""
    try:
        computers = (await asyncio.to_thread(kvm_config.read))["computers"]
    except kvm_config.ConfigError as exc:
        raise HTTPException(503, str(exc)) from exc
    return await asyncio.to_thread(monitors.view, computers, peer_control.desktop_peers())


@app.get("/kvm/config")
async def kvm_config_read():
    try:
        return await asyncio.to_thread(kvm_config.read)
    except kvm_config.ConfigError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/kvm/config/layout")
async def kvm_config_layout(body: dict):
    return await asyncio.to_thread(_kvm_config_call, kvm_config.save_layout, body.get("cells"), body.get("hotkeys"),
                                   body.get("hotkey_actions"), body.get("layout"))


@app.post("/kvm/config/layout/preview")
async def kvm_config_layout_preview(body: dict):
    """Where a monitor layout's computers touch and what that would link
    (app/kvm_layout.py), for the editor to draw before saving."""
    try:
        return await asyncio.to_thread(kvm_config.preview, body.get("layout"))
    except kvm_config.ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/kvm/config/setups/use")
async def kvm_config_use_setup(body: dict):
    """Puts a layout saved for a setup in use by hand."""
    return await asyncio.to_thread(_kvm_config_call, kvm_config.use_setup, body.get("key"))


@app.post("/kvm/config/setups/delete")
async def kvm_config_delete_setup(body: dict):
    return await asyncio.to_thread(_kvm_config_call, kvm_config.delete_setup, body.get("key"))


@app.post("/kvm/config/computers")
async def kvm_config_computers(body: dict):
    return await asyncio.to_thread(_kvm_config_call, kvm_config.save_computers, body.get("computers"))


@app.post("/kvm/config/input")
async def kvm_config_input(body: dict):
    return await asyncio.to_thread(_kvm_config_call, kvm_config.save_input, body.get("input"))


@app.get("/kvm/bindings")
def kvm_bindings():
    """Key and button remapping in the KVM's input path (app/key_bindings.py)."""
    return key_bindings.read()


@app.post("/kvm/bindings")
async def kvm_bindings_save(body: dict):
    try:
        note = await asyncio.to_thread(key_bindings.save, body.get("bindings"))
    except key_bindings.BindingError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**key_bindings.read(), "note": note}


@app.post("/kvm/bindings/identify")
def kvm_bindings_identify(body: dict):
    """While on, the KVM reports the last key or button pressed."""
    key_bindings.identify(bool(body.get("on")))
    return key_bindings.read()


def _updates_section(request: Request, error: str | None = None, since: str | None = None):
    status = updates.status()
    response = templates.TemplateResponse(request, "_updates.html",
                                          {"updates": status, "error": error, "since": since})
    if since and status["running"] and since != status["running"] and status["state"] != "running":
        # Pages from 0.3.1/0.3.2 poll with ?since=; reload them on the new
        # version. Newer pages reload on the websocket's "hello" instead.
        response.headers["HX-Refresh"] = "true"
    return response


@app.get("/updates", response_class=HTMLResponse)
def updates_section(request: Request, since: str | None = None):
    """The Settings > Updates section; `since` is the version the polling
    page was loaded on."""
    return _updates_section(request, since=since)


@app.post("/updates/{action}", response_class=HTMLResponse)
def run_update(request: Request, action: str):
    """Asks the root updater (packaging/bragi-update) to check or upgrade."""
    try:
        updates.request(action)
    except updates.UpdateError as exc:
        return _updates_section(request, str(exc))
    return _updates_section(request)


# --- Settings > This hub (app/hub_info.py) ---

def _hub_section(request: Request, error: str | None = None, note: str | None = None):
    view = {"info": hub_info.message(), "clients": sorted(peer_control.desktop_peers())}
    return templates.TemplateResponse(request, "_hub.html", {"hub": view, "error": error, "note": note})


@app.get("/hub", response_class=HTMLResponse)
def hub_section(request: Request):
    """What tray apps are told about this hub, and moving them elsewhere."""
    return _hub_section(request)


@app.post("/hub/move", response_class=HTMLResponse)
def hub_move(request: Request, url: str = Form(...)):
    try:
        count = peer_control.move_clients(url)
    except ValueError as exc:
        return _hub_section(request, error=str(exc))
    target = hub_info.ws_url(url)
    return _hub_section(request, note=f"Told {count} tray app{'s' if count != 1 else ''} to switch to {target}."
                        if count else "No tray app is connected, so nothing was moved.")


# --- Settings > Setup (app/setup_checks.py) ---

def _setup_section(request: Request, error: str | None = None, note: str | None = None):
    return templates.TemplateResponse(request, "_setup.html",
                                      {"setup": setup_checks.view(), "error": error, "note": note})


@app.get("/setup", response_class=HTMLResponse)
def setup_section(request: Request):
    """Host checks: PipeWire, Roc/VBAN modules, lingering, input group,
    HyperX quirk, tailscale serve, Bragi's services."""
    return _setup_section(request)


@app.post("/setup/fix/{check_id}", response_class=HTMLResponse)
def setup_fix(request: Request, check_id: str):
    try:
        note = setup_checks.fix(check_id)
    except setup_checks.SetupError as exc:
        return _setup_section(request, error=str(exc))
    return _setup_section(request, note=note)


# --- mouse buttons (app/hidpp.py), in the KVM setup ---

def _mouse_buttons_view() -> dict:
    return {"devices": hidpp.manager.devices() if hidpp.manager else [],
            "computers": actions.computers(), "fork": kvm_config.fork_hidpp()}


@app.get("/mouse-buttons")
def mouse_buttons():
    return _mouse_buttons_view()


@app.post("/mouse-buttons")
async def mouse_buttons_set(request: Request):
    body = await request.json()
    if hidpp.manager is None:
        raise HTTPException(503, "Mouse buttons aren't running in this Bragi.")
    try:
        device, cid = str(body["device"]), int(str(body["control"]), 0)
        await hidpp.manager.set_action(device, cid, body.get("action"))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc) or "device, control and action") from exc
    return _mouse_buttons_view()


@app.post("/mouse-buttons/hand-over")
async def mouse_buttons_hand_over():
    """The KVM server stops handling HID++ buttons itself."""
    try:
        note = await asyncio.to_thread(kvm_config.hand_over_hidpp)
    except kvm_config.ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**_mouse_buttons_view(), "note": note}


# --- the controller test (app/gamepad.py) ---

def _hub() -> gamepad.Hub:
    if gamepad.hub is None:
        raise HTTPException(503, "Controllers aren't running in this Bragi.")
    return gamepad.hub


@app.post("/gamepads/watch")
def gamepads_watch():
    """The test dialog is open: live "gamepad_state" on the websocket for a
    while, and every controller's state now."""
    return {"controllers": _hub().watch()}


@app.post("/gamepads/{controller_id}/rumble")
def gamepads_rumble(controller_id: str, strength: Literal["light", "strong"] = "strong"):
    """Vibrates the controller for a moment, on the box itself."""
    strong, weak = (0x4000, 0x8000) if strength == "light" else (0xFFFF, 0xC000)
    error = _hub().test_rumble(controller_id, strong, weak, 600)
    if error:
        raise HTTPException(400, error)
    return {"ok": True}


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


@app.post("/bluetooth/extras", response_class=HTMLResponse)
async def bluetooth_extras(request: Request):
    """What the controllers' extra buttons do: each field is "" (nothing),
    a button's name, or "keys" with the combination in <name>_keys."""
    hub = _hub()
    form = await request.form()
    current = hub.extras()
    new, error = {}, None
    for name, label in gamepad.EXTRA_LABELS.items():
        choice = str(form.get(name, ""))
        if choice == "keys":
            # Just switched to keys: start from what it pressed before, or the screenshot.
            keys = str(form.get(f"{name}_keys", "")).strip() or current.get(name, {}).get("keys") \
                or gamepad.DEFAULT_EXTRAS["share"]["keys"]
            value = {"keys": keys}
        else:
            value = {"button": choice} if choice else {}
        try:
            new[name] = gamepad.valid_extra(value)
        except ValueError as exc:
            error = f"{label}: {exc}"
    if error is None:
        hub.set_extras(new)
    return await _bluetooth_view(request, extras_error=error)


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
