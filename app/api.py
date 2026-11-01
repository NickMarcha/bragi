"""The HTTP API for other apps: list devices, read and set their volume,
mute and balance, and the /api/v1/help explainer.

Authentication is done before a request gets here, by access.py: every
path under /api/v1 except /help needs an API key made in the dashboard's
Security dialog.

A write goes through ws.set_control, so every open dashboard (and the tray
app's volume window) sees the change the moment it is made.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, model_validator

from . import views, ws

router = APIRouter(prefix="/api/v1")

_LABELS = {
    "playback": "Speakers", "capture": "Mic",
    "outgoing": "Mic to {name}", "incoming": "{name} to headset",
}


def _control(view: dict, direction: str, name: str, pannable: bool) -> dict:
    control = {
        "direction": direction,
        "label": _LABELS[direction].format(name=name),
        "volume": view["volume"],
        "muted": view["muted"],
        "connected": view["connected"],
        "pannable": pannable,
    }
    if pannable:
        control["balance"] = view["balance"]
    return control


def headset_device(view: dict) -> dict:
    return {
        "id": f"headset:{view['key']}",
        "kind": "headset",
        "key": view["key"],
        "name": view["label"],
        "enabled": view["enabled"],
        "controls": {d: _control(view[d], d, view["label"], False) for d in ("playback", "capture")},
    }


def peer_device(view: dict) -> dict:
    return {
        "id": f"peer:{view['name']}",
        "kind": "peer",
        "key": view["name"],
        "name": view["name"],
        "protocol": view["protocol"],
        "client_connected": view["client_connected"],
        "controls": {d: _control(view[d], d, view["name"], True) for d in ("outgoing", "incoming")},
    }


def _devices() -> list[dict]:
    state = views.build_state()
    return [headset_device(h) for h in state["headsets"]] + [peer_device(p) for p in state["peers"]]


def _kind(kind: str) -> str:
    if kind not in ws.DIRECTIONS:
        raise HTTPException(404, "Device kind is 'headset' or 'peer'")
    return kind


@router.get("/help", response_class=PlainTextResponse)
def explainer(request: Request):
    """How to use this API and the WebSocket, as Markdown. Public."""
    base = _base_url(request)
    text = HELP.replace("{base_ws}", "ws" + base[len("http"):]).replace("{base}", base)
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


@router.get("")
def root():
    return {"help": "/api/v1/help", "devices": "/api/v1/devices", "websocket": "/ws"}


@router.get("/devices")
async def list_devices():
    return {"devices": await asyncio.to_thread(_devices)}


@router.get("/devices/{kind}/{key}")
async def get_device(kind: str, key: str):
    device_id = f"{_kind(kind)}:{key}"
    device = next((d for d in await asyncio.to_thread(_devices) if d["id"] == device_id), None)
    if device is None:
        raise HTTPException(404, f"No device {device_id!r}")
    return device


class ControlChange(BaseModel):
    volume: float | None = Field(default=None, ge=0.0, le=1.5,
                                 description="1.0 is 100%, the most is 1.5 (150%)")
    muted: bool | None = None
    balance: float | None = Field(default=None, ge=-1.0, le=1.0,
                                  description="-1 full left, 0 centre, 1 full right. Peers only.")

    @model_validator(mode="after")
    def something_to_change(self):
        if self.volume is None and self.muted is None and self.balance is None:
            raise ValueError("Send at least one of volume, muted, balance")
        return self


@router.patch("/devices/{kind}/{key}/controls/{direction}")
async def change_control(kind: str, key: str, direction: str, change: ControlChange):
    kind = _kind(kind)
    try:
        message = await ws.set_control(kind, key, direction, volume=change.volume,
                                       muted=change.muted, balance=change.balance)
    except ws.ControlNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ws.ControlOffline as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "device": f"{kind}:{key}",
        "direction": direction,
        "requested": change.model_dump(exclude_none=True),
        "control": _control(message, direction, key, kind == "peer"),
    }


class DeviceChange(BaseModel):
    enabled: bool


@router.patch("/devices/headset/{key}")
async def change_headset(key: str, change: DeviceChange):
    try:
        view = await ws.set_headset_enabled(key, change.enabled)
    except ws.ControlNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ws.ControlOffline as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"requested": change.model_dump(), "device": headset_device(view)}


def _base_url(request: Request) -> str:
    """The address the caller used, so the examples work when pasted.
    Behind `tailscale serve` the scheme comes from X-Forwarded-Proto."""
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


HELP = """\
# Bragi API

Bragi is the audio mixer on sagepi. This API lets another app list its
audio devices, read their volume, mute and balance, and change them. Every
change shows up live in any open dashboard.

Base URL: {base}

## Authentication

Make an API key in the dashboard: the lock button in the top bar opens
Security, then "API connections". The key is shown once. Send it on every
request in one of these ways:

    Authorization: Bearer <key>
    X-API-Key: <key>
    ?token=<key>          (query string; meant for WebSockets)

A missing or wrong key gets `401`. This page and nothing else is public.

## Devices and controls

A device is a USB headset plugged into sagepi (`kind: headset`) or a
network peer (`kind: peer`). Each device has two controls, one per
direction:

| kind    | direction  | what it is                                 |
|---------|------------|--------------------------------------------|
| headset | `playback` | the headset's speakers                     |
| headset | `capture`  | the headset's mic                          |
| peer    | `outgoing` | sagepi's mic as the peer hears it          |
| peer    | `incoming` | the peer's audio in sagepi's headset mix   |

Each control has:

- `volume`: linear gain, `0.0` silent, `1.0` is 100%, `1.5` the most
  (150%). `null` when not connected. The dashboard's dB readout is
  `20 * log10(volume)`.
- `muted`: `true` or `false`.
- `connected`: whether the control has a live audio node right now. A
  disabled headset or a peer that is not streaming is not connected, and
  cannot be changed until it is.
- `balance` (peer controls only, `pannable: true`): `-1.0` full left,
  `0.0` centre, `1.0` full right. Headsets have no balance; their hardware
  mixer does not keep it.

## Endpoints

### GET /api/v1/devices

Every device with every control.

    curl -H "Authorization: Bearer $BRAGI_KEY" {base}/api/v1/devices

```json
{"devices": [
  {"id": "headset:usb-headset", "kind": "headset", "key": "usb-headset",
   "name": "USB Headset", "enabled": true,
   "controls": {
     "playback": {"direction": "playback", "label": "Speakers", "volume": 0.8,
                  "muted": false, "connected": true, "pannable": false},
     "capture":  {"direction": "capture", "label": "Mic", "volume": 1.0,
                  "muted": false, "connected": true, "pannable": false}}},
  {"id": "peer:sage", "kind": "peer", "key": "sage", "name": "sage",
   "protocol": "vban", "client_connected": null,
   "controls": {
     "outgoing": {"direction": "outgoing", "label": "Mic to sage", "volume": 1.0,
                  "muted": false, "connected": true, "pannable": true, "balance": 0.0},
     "incoming": {"direction": "incoming", "label": "sage to headset", "volume": 0.6,
                  "muted": false, "connected": true, "pannable": true, "balance": -0.2}}}
]}
```

`client_connected` is whether the peer's companion app (tray app or
Android app) is online; `null` for peers that have none.

### GET /api/v1/devices/{kind}/{key}

One device, same shape. `404` if there is no such device.

### PATCH /api/v1/devices/{kind}/{key}/controls/{direction}

Change one control. Send any of `volume`, `muted`, `balance`; leave out
what should stay as it is. Values are absolute, not steps.

    curl -X PATCH -H "Authorization: Bearer $BRAGI_KEY" \\
         -H "Content-Type: application/json" \\
         -d '{"volume": 0.5, "muted": false}' \\
         {base}/api/v1/devices/peer/sage/controls/incoming

The response is the control as read back from PipeWire after the change,
next to what you asked for:

```json
{"device": "peer:sage", "direction": "incoming",
 "requested": {"volume": 0.5, "muted": false},
 "control": {"direction": "incoming", "label": "sage to headset", "volume": 0.5,
             "muted": false, "connected": true, "pannable": true, "balance": -0.2}}
```

Errors: `404` no such device or direction, `409` the control is not
connected right now, `422` a value out of range, `balance` on a headset,
or nothing to change.

### PATCH /api/v1/devices/headset/{key}

Switch a headset on or off: `{"enabled": false}`. Off releases the USB
device entirely (no audio nodes), not just mute. Returns the device.

## Live updates: the WebSocket

    {base_ws}/ws?token=<key>

This is the socket the dashboard itself uses. Messages are JSON objects
with a `type`.

On connect, the server sends one `state` message: the whole picture.

    {"type": "state", "headsets": [...], "peers": [...], "order_of": {...},
     "viz_settings": {"enabled": true}, "microphone": {...}}

Its `headsets[]` and `peers[]` hold the raw views the REST devices are made
from: a headset has `key`, `label`, `enabled`, `playback`, `capture`; a
peer has `name`, `protocol`, `outgoing`, `incoming`. Each direction has
`volume`, `muted`, `connected`, `balance`.

After that, the server sends only what changes:

- `control`: one control changed, by anyone (the dashboard, this API, a
  hardware volume knob).
  `{"type": "control", "target": "peer", "key": "sage", "direction": "incoming",
    "volume": 0.5, "muted": false, "connected": true, "balance": -0.2, "ts": null}`
- `headset`: a headset was switched on or off; carries the full headset view.
- `levels`: live signal meters, about 20 a second, while level meters are
  on (the dashboard's visualizer setting). `value` is the meter's height,
  `0.0` to `1.0`, not the volume setting.
  `{"type": "levels", "values": [{"target": "headset", "key": "usb-headset",
    "direction": "playback", "value": 0.42}, ...]}`
- `peer_presence`, `peer_status`, `peer_streams`, `peer_registry`,
  `microphone`, `order`, `viz_settings`: dashboard housekeeping. Safe to
  ignore. `peer_registry` means peers were added or removed: fetch
  `/api/v1/devices` again.

You can also send actions on the socket. `target` is `headset` or `peer`,
`key` the device key, `direction` as above:

    {"action": "set_volume", "target": "peer", "key": "sage", "direction": "incoming", "value": 0.5}
    {"action": "set_mute",   "target": "peer", "key": "sage", "direction": "incoming", "value": true}
    {"action": "toggle_mute","target": "peer", "key": "sage", "direction": "incoming"}
    {"action": "set_balance","target": "peer", "key": "sage", "direction": "incoming", "value": -0.2}
    {"action": "toggle_enabled", "target": "headset", "key": "usb-headset"}

Actions get no direct reply; the resulting `control` (or `headset`)
message is the confirmation. `set_volume` and `set_balance` are
rate-limited to one write per control every 100 ms, keeping the newest
value, so sending one per frame of a drag is fine. They take an optional
`ts`: if you send it, make it `Date.now()`-style milliseconds that only
ever increase per control, because anything older than the newest `ts`
already seen for that control is dropped.

Use REST for one-off changes where you want the result back, and the
WebSocket to follow changes live or drive a fader.

## Also

- `{base}/docs` is the generated OpenAPI page for every route (it needs
  the dashboard login if a password is set).
- Peers' own apps (the Android app, the desktop tray app) use
  `/api/peers/...` and `/ws/peer/...`. Those are not part of this API.
"""

