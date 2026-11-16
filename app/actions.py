"""Bragi's actions for buttons: the headset's macro button now, the mouse's
extra buttons next (step 3 of desk-hub-vision.md).

The KVM actions run through deskflow-core's own socket (app/kvm.py):

- kvm_switch: make one computer the active one,
- kvm_next: the next connected computer, in the KVM setup's order,
- kvm_keys: press a key combination on some computers (none listed: the
  active one), e.g. Discord's mute on all of them. A listed computer whose
  Bragi tray app can press keys itself gets them from the tray app; the
  others through the KVM. Linux clients on Wayland drop KVM keys while the
  cursor is on another computer (deskflow/deskflow#10393), which the tray
  app gets round.

macro_button.py adds its own kinds (an HTTP request, mic monitoring) on top.
"""

from __future__ import annotations

import asyncio

from . import key_bindings, kvm, kvm_config, peer_control

KVM_KINDS = ("kvm_switch", "kvm_next", "kvm_keys")
_MAX_NAME = 63


def _name(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _MAX_NAME \
            or any(c in value for c in ":,()=\n\r"):
        raise ValueError(f"{what} is a computer's name")
    return value.strip()


def valid_kvm(action: dict) -> dict:
    """A KVM action as it's saved; ValueError for one that couldn't run."""
    kind = action.get("type")
    if kind == "kvm_switch":
        return {"type": kind, "computer": _name(action.get("computer"), "computer")}
    if kind == "kvm_next":
        return {"type": kind}
    if kind == "kvm_keys":
        keys = action.get("keys")
        if not isinstance(keys, str) or not keys.strip() or len(keys) > 100 or any(c in keys for c in ",()=\n\r"):
            raise ValueError('keys look like "Control+Shift+Alt+M"')
        computers = action.get("computers") or []
        if not isinstance(computers, list) or len(computers) > 32:
            raise ValueError("computers is a list of names")
        names = []
        for c in computers:
            name = _name(c, "each computer")
            if name not in names:
                names.append(name)
        return {"type": kind, "keys": keys.strip(), "computers": names}
    raise ValueError(f"unknown action type {kind!r}")


def computers() -> list[str]:
    """The KVM's computers in the setup's order, for the forms."""
    try:
        return [c["name"] for c in kvm_config.read()["computers"]]
    except Exception:
        return list(kvm.read_view()["computers"])


def next_computer(order: list[str], connected: list[str], active: str | None) -> str | None:
    """The connected computer after the active one, in the setup's order
    (connected ones the setup doesn't list come last), wrapping round."""
    ring = [c for c in order if c in connected] + [c for c in connected if c not in order]
    if not ring:
        return None
    if active not in ring:
        return ring[0]
    return ring[(ring.index(active) + 1) % len(ring)]


def _run_kvm(action: dict) -> str:
    kind = action["type"]
    if kind == "kvm_switch":
        kvm.switch(action["computer"])
        return f"switched to {action['computer']}"
    if kind == "kvm_next":
        view = kvm.read_view()
        target = next_computer(computers(), view["computers"], view["active"])
        if target is None:
            raise kvm.KvmError("No computer is connected.")
        if target != view["active"]:
            kvm.switch(target)
        return f"switched to {target}"
    targets = action["computers"]
    if not targets:
        kvm.keystroke(action["keys"], None)
        return f"pressed {action['keys']} on the active computer"
    if targets == ["*"]:
        targets = computers()
    by_app, by_kvm = split_by_tray_app(action["keys"], targets)
    for peer, codes in by_app:
        peer_control.press_keys(peer, codes)
    if by_kvm:
        kvm.keystroke(action["keys"], by_kvm)
    done = [f"{peer} (Bragi app)" for peer, _ in by_app] + by_kvm
    return f"pressed {action['keys']} on " + ", ".join(done)


def split_by_tray_app(keys: str, targets: list[str]) -> tuple[list[tuple[str, list[int]]], list[str]]:
    """Which computers get the keys from their tray app (peer, evdev codes)
    and which through the KVM. Keys the tray apps' table doesn't know all go
    through the KVM."""
    try:
        codes = key_bindings.parse_combo(keys)
    except key_bindings.BindingError:
        return [], targets
    try:
        setup = kvm_config.read()["computers"]
    except Exception:
        setup = []
    by_app, by_kvm = [], []
    for computer in targets:
        peer = peer_control.peer_for_computer(computer, setup)
        if peer is not None and peer_control.has_feature(peer, "keys"):
            by_app.append((peer, codes))
        else:
            by_kvm.append(computer)
    return by_app, by_kvm


async def run_kvm(action: dict) -> tuple[bool, str]:
    """Runs a KVM action in a worker thread: (worked, what happened)."""
    try:
        return True, await asyncio.to_thread(_run_kvm, action)
    except kvm.KvmError as exc:
        return False, str(exc)


def migrate_webui_keystroke(action: dict) -> dict:
    """The macro button used to press keys through the old Deskflow web UI
    (an HTTP request to its socket's /api/keystroke). That page is going
    away, so such an action reads as the kvm_keys action it was."""
    import json
    if action.get("type") != "http" or action.get("target") != "socket" or action.get("path") != "/api/keystroke" \
            or not str(action.get("socket", "")).endswith("deskflow-headless/webui.sock"):
        return action
    try:
        body = json.loads(action.get("body") or "{}")
        return valid_kvm({"type": "kvm_keys", "keys": body.get("keys"), "computers": body.get("computers") or []})
    except (ValueError, AttributeError):
        return action
