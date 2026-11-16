"""Keyboard hotkeys that run Bragi actions.

The KVM server sees the keyboard, so it spots the hotkey: a line like
`keystroke(Control+Alt+F8) = notify(bragi-1)` in its config makes it
broadcast `action=bragi-1` on its IPC socket (the fork's notify action).
Bragi keeps a connection open (kvm.listen_actions) and runs the action
saved for that id here, in data/hotkey_actions.yaml: the same actions as
the mouse and headset buttons (actions.py). That way a hotkey's keys for
other computers go through their tray apps, which Wayland clients need
(deskflow/deskflow#10393).
"""

from __future__ import annotations

import logging
import re
import threading
import time

import yaml

from . import actions, peers

logger = logging.getLogger("bragi.hotkeys")

SETTINGS_FILE = peers.DATA_DIR / "hotkey_actions.yaml"
ID = re.compile(r"^bragi-\d{1,4}$")
NOTIFY = re.compile(r"=\s*notify\((bragi-\d{1,4})\)\s*$")

_lock = threading.Lock()
_results: dict[str, dict] = {}
_running: set[str] = set()


def load() -> dict[str, dict]:
    with _lock:
        if not SETTINGS_FILE.exists():
            return {}
        data = yaml.safe_load(SETTINGS_FILE.read_text()) or {}
    return {k: v for k, v in data.items() if isinstance(k, str) and ID.match(k) and isinstance(v, dict)} \
        if isinstance(data, dict) else {}


def ids_in(hotkeys: list[str]) -> list[str]:
    return [m.group(1) for line in hotkeys if (m := NOTIFY.search(line))]


def check(hotkeys: list[str], wanted) -> dict[str, dict]:
    """The actions for the notify ids the hotkey lines use, checked; ids no
    line uses are dropped."""
    if wanted is None:
        wanted = load()
    if not isinstance(wanted, dict):
        raise ValueError("actions is an object: id -> action")
    out = {}
    for hotkey_id in ids_in(hotkeys):
        action = wanted.get(hotkey_id)
        if not isinstance(action, dict):
            raise ValueError(f"The hotkey {hotkey_id} has no action")
        out[hotkey_id] = actions.valid_kvm(action)
    return out


def save(saved: dict[str, dict]) -> None:
    with _lock:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        pending = SETTINGS_FILE.with_suffix(".tmp")
        pending.write_text(yaml.safe_dump(saved, sort_keys=False))
        pending.replace(SETTINGS_FILE)


def results() -> dict[str, dict]:
    return dict(_results)


async def run(name: str) -> None:
    """A notify(name) hotkey fired in the KVM server."""
    action = load().get(name)
    if action is None or name in _running:
        return
    _running.add(name)
    try:
        ok, text = await actions.run_kvm(action)
    except Exception as exc:
        logger.exception("hotkey %s failed", name)
        ok, text = False, str(exc) or type(exc).__name__
    finally:
        _running.discard(name)
    if not ok:
        logger.warning("hotkey %s: %s", name, text)
    _results[name] = {"ok": ok, "text": text, "at": time.time()}
