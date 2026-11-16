"""Key and button remapping in the KVM server's input path (bindings.json).

The KVM server reads the keyboards and mice itself, so remapping a key has
to happen there: a binding makes one key or button send other key codes
instead (to the active computer), e.g. a mouse's side button sending
Control+Super+Right for the next virtual desktop, or a hotkey from the KVM
setup. The server reads ~/.config/deskflow-headless/bindings.json again on
the next press after it changes.

Which key was pressed comes from the server too: while the "identify" file
in its status directory is fresh (under a minute old), it reports the last
key or button pressed, with its device, as `lastInput` in status.json.

Ported from the fork's setup page (headless/webui), which is going away.
Its "remaps" (Logitech controls acting as other controls) are kept as they
are; Bragi's mouse buttons (hidpp.py) cover those controls now.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from . import kvm, kvm_config

MAX_BINDINGS = 100
HIDPP_CODE_BASE = kvm_config.HIDPP_CODE_BASE

# Linux evdev codes, named like Deskflow's config (Control+Alt+F12).
KEY_CODES: dict[str, int] = {
    "Escape": 1, "Minus": 12, "Equal": 13, "BackSpace": 14, "Tab": 15, "BracketLeft": 26, "BracketRight": 27,
    "Return": 28, "Control": 29, "Semicolon": 39, "Apostrophe": 40, "Grave": 41, "Shift": 42, "Backslash": 43,
    "Comma": 51, "Period": 52, "Slash": 53, "Alt": 56, "Space": 57, "CapsLock": 58, "F11": 87, "F12": 88,
    "AltGr": 100, "Home": 102, "Up": 103, "PageUp": 104, "Left": 105, "Right": 106, "End": 107, "Down": 108,
    "PageDown": 109, "Insert": 110, "Delete": 111, "AudioMute": 113, "AudioLowerVolume": 114,
    "AudioRaiseVolume": 115, "Pause": 119, "Super": 125, "Menu": 127, "AudioNext": 163, "AudioPlay": 164,
    "AudioPrev": 165, "Print": 99,
    "MouseLeft": 272, "MouseRight": 273, "MouseMiddle": 274, "MouseBack": 275, "MouseForward": 276,
    "MouseSide2": 277, "MouseExtra2": 278, "MouseTask": 279,
}
for _i, _c in enumerate("QWERTYUIOP"):
    KEY_CODES[_c] = 16 + _i
for _i, _c in enumerate("ASDFGHJKL"):
    KEY_CODES[_c] = 30 + _i
for _i, _c in enumerate("ZXCVBNM"):
    KEY_CODES[_c] = 44 + _i
for _n in range(1, 10):
    KEY_CODES[str(_n)] = _n + 1
KEY_CODES["0"] = 11
for _n in range(1, 11):
    KEY_CODES[f"F{_n}"] = 58 + _n
for _n in range(13, 25):
    KEY_CODES[f"F{_n}"] = 170 + _n
ALIASES = {"ctrl": "Control", "win": "Super", "meta": "Super", "cmd": "Super", "enter": "Return",
           "esc": "Escape", "del": "Delete"}
_BY_LOWER = {name.lower(): name for name in KEY_CODES}
_NAME_OF = {code: name for name, code in KEY_CODES.items()}
HIDPP_NAMES = {0xC3: "Thumb button", 0xC4: "Wheel mode button", 0xD7: "Gesture button"}


class BindingError(ValueError):
    pass


def parse_combo(text: str) -> list[int]:
    """'Control+Super+Right' -> evdev codes."""
    parts = [p.strip() for p in str(text).split("+") if p.strip()]
    if not parts or len(parts) > 8:
        raise BindingError("Type what to send, e.g. Control+Super+Right (up to 8 keys).")
    codes = []
    for part in parts:
        name = ALIASES.get(part.lower()) or _BY_LOWER.get(part.lower())
        if name is None:
            raise BindingError(f'Unknown key "{part}".')
        codes.append(KEY_CODES[name])
    return codes


def input_name(code: int) -> str:
    if code >= HIDPP_CODE_BASE:
        return HIDPP_NAMES.get(code - HIDPP_CODE_BASE, f"control 0x{code - HIDPP_CODE_BASE:x}")
    return _NAME_OF.get(code) or (f"button 0x{code:x}" if code >= 0x100 else f"key {code}")


def _path() -> Path:
    return kvm_config.config_dir() / "bindings.json"


def _data() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _identify_file() -> Path:
    return kvm.engine_dir() / "identify"


def read() -> dict:
    """The bindings, named, and the last input the server reported."""
    bindings = []
    for b in _data().get("bindings", []):
        if isinstance(b, dict) and isinstance(b.get("code"), int) and isinstance(b.get("send"), list):
            bindings.append({"device": str(b.get("device", "")), "code": b["code"], "input": input_name(b["code"]),
                             "send": b["send"], "sends": "+".join(input_name(c) for c in b["send"]
                                                                  if isinstance(c, int)),
                             "label": str(b.get("label", ""))})
    try:
        status = json.loads((kvm.engine_dir() / "status.json").read_text())
    except (OSError, ValueError):
        status = {}
    last = status.get("lastInput") if isinstance(status, dict) else None
    if isinstance(last, dict) and isinstance(last.get("code"), int):
        last = {"at": last.get("at"), "device": str(last.get("device", "")), "code": last["code"],
                "input": input_name(last["code"])}
    else:
        last = None
    return {"bindings": bindings, "last_input": last, "identifying": _identify_file().exists()}


_lock = threading.Lock()


def save(bindings) -> str:
    """Replaces the bindings (the remaps stay). Each is {device (part of a
    device's name, "" for any), code (the input), send: [codes] or keys:
    "Control+Super+Right", label}."""
    if not isinstance(bindings, list) or len(bindings) > MAX_BINDINGS:
        raise BindingError(f"bindings is a list (at most {MAX_BINDINGS})")
    out, seen = [], set()
    for b in bindings:
        if not isinstance(b, dict):
            raise BindingError("each binding is an object")
        device = str(b.get("device", "")).strip()
        code = b.get("code")
        if len(device) > 128 or "\n" in device:
            raise BindingError("the device name is too long")
        # evdev codes, or 0x1000 + control id for Logitech controls over HID++
        if not isinstance(code, int) or isinstance(code, bool) or not 0 < code < 0x2000:
            raise BindingError(f"bad input code: {code!r}")
        send = parse_combo(b["keys"]) if b.get("keys") else b.get("send")
        if not (isinstance(send, list) and 0 < len(send) <= 8
                and all(isinstance(c, int) and not isinstance(c, bool) and 0 < c < 0x300 for c in send)):
            raise BindingError("each binding sends 1 to 8 keys or buttons")
        if (device, code) in seen:
            raise BindingError(f"{input_name(code)} on {device or 'any device'} is bound twice")
        seen.add((device, code))
        label = str(b.get("label") or "+".join(input_name(c) for c in send))[:80]
        out.append({"device": device, "code": code, "send": send, "label": label})
    with _lock:
        data = _data()
        data["bindings"] = out
        data.setdefault("remaps", [])
        kvm_config._backup(_path())
        kvm_config._atomic_write(_path(), json.dumps(data, indent=2) + "\n")
    return "Saved. The KVM picks it up on the next key or button press."


def identify(on: bool) -> None:
    """While on (and refreshed within a minute), the server reports each
    press as lastInput."""
    path = _identify_file()
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    else:
        path.unlink(missing_ok=True)
