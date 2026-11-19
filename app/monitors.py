"""The KVM computers' monitors, as their tray apps report them.

Step 1 of the layout work in docs/kvm-layout.md. Each Bragi tray app sends
a "monitors" message after it connects and whenever its monitors change
(client/src/Bragi.Client/Monitors/): every enabled monitor's rectangle in
that computer's desktop coordinates, its mode, scaling and rotation, and
what its EDID says (an id that recognises the same monitor on any computer,
its name and size). A monitor showing a copy of another (KDE's "Replica
of", Windows' "Duplicate") is listed under that one as a mirror: no desktop
area of its own, but known.

Kept here: the latest report from each computer, and every monitor ever
seen, by id, with the computer it was last on. Both are saved to
data/monitors.yaml, so the layout knows a computer's monitors while it's
off, and knows the TV when its cable moves to another computer.
"""

from __future__ import annotations

import logging
import threading
import time

import yaml

from . import peers

logger = logging.getLogger("bragi.monitors")

SETTINGS_FILE = peers.DATA_DIR / "monitors.yaml"
MAX_MONITORS = 16
MAX_MIRRORS = 8
_INT_FIELDS = ("x", "y", "width", "height", "pixel_width", "pixel_height")

_lock = threading.Lock()
# What changed is told to the dashboards: (message) -> None.
broadcast = None
# Told when any computer's monitors change, to follow the setup (app/kvm_follow.py).
on_change = None


class MonitorError(ValueError):
    pass


def _text(value, limit: int = 64) -> str:
    if not isinstance(value, str):
        raise MonitorError("expected text")
    return value.strip()[:limit]


def _int(value, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise MonitorError("number out of range")
    return value


def _size_mm(m: dict, item: dict) -> None:
    for key in ("width_mm", "height_mm"):
        value = m.get(key)
        item[key] = _int(value, 1, 10_000) if value is not None else None


def _mirror(m) -> dict:
    if not isinstance(m, dict):
        raise MonitorError("a mirror is an object")
    item = {"id": _text(m.get("id")), "name": _text(m.get("name")), "connector": _text(m.get("connector")),
            "pixel_width": _int(m.get("pixel_width"), 1, 100_000), "pixel_height": _int(m.get("pixel_height"), 1, 100_000)}
    if not item["id"]:
        raise MonitorError("a mirror needs an id")
    _size_mm(m, item)
    return item


def normalise(message: dict) -> dict:
    """A "monitors" message, checked and reduced to what's kept."""
    monitors = message.get("monitors")
    if not isinstance(monitors, list) or len(monitors) > MAX_MONITORS:
        raise MonitorError("monitors is a list")
    out = []
    for m in monitors:
        if not isinstance(m, dict):
            raise MonitorError("a monitor is an object")
        item = {"id": _text(m.get("id")), "name": _text(m.get("name")), "connector": _text(m.get("connector"))}
        if not item["id"]:
            raise MonitorError("a monitor needs an id")
        for key in _INT_FIELDS:
            item[key] = _int(m.get(key), -100_000 if key in ("x", "y") else 1, 100_000)
        scale = m.get("scale")
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0.25 <= scale <= 8:
            raise MonitorError("scale out of range")
        item["scale"] = round(float(scale), 3)
        item["rotation"] = m.get("rotation") if m.get("rotation") in (0, 90, 180, 270) else 0
        item["primary"] = m.get("primary") is True
        _size_mm(m, item)
        mirrors = m.get("mirrors") or []
        if not isinstance(mirrors, list) or len(mirrors) > MAX_MIRRORS:
            raise MonitorError("mirrors is a list")
        item["mirrors"] = [_mirror(r) for r in mirrors]
        out.append(item)
    return {"source": _text(message.get("source") or "unknown", 16), "monitors": out}


def _load() -> dict:
    if not SETTINGS_FILE.exists():
        return {"computers": {}, "known": {}}
    data = yaml.safe_load(SETTINGS_FILE.read_text()) or {}
    return {"computers": data.get("computers") or {}, "known": data.get("known") or {}}


def _save(data: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    pending = SETTINGS_FILE.with_suffix(".tmp")
    pending.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    pending.replace(SETTINGS_FILE)


def report(peer: str, message: dict, now: float | None = None) -> bool:
    """A tray app's monitors. True when they differ from what was kept."""
    report = normalise(message)
    now = time.time() if now is None else now
    with _lock:
        data = _load()
        old = data["computers"].get(peer, {})
        changed = old.get("monitors") != report["monitors"] or old.get("source") != report["source"]
        data["computers"][peer] = {**report, "seen": int(now)}
        for m in report["monitors"]:
            data["known"][m["id"]] = {"name": m["name"], "width_mm": m["width_mm"], "height_mm": m["height_mm"],
                                      "computer": peer, "mirror_of": None, "seen": int(now)}
            for r in m["mirrors"]:
                data["known"][r["id"]] = {"name": r["name"], "width_mm": r["width_mm"], "height_mm": r["height_mm"],
                                          "computer": peer, "mirror_of": m["id"], "seen": int(now)}
        _save(data)
    if changed:
        logger.info("monitors: %s has %s", peer, ", ".join(
            f"{m['name']} {m['pixel_width']}x{m['pixel_height']}"
            + "".join(f" (mirrored on {r['name']})" for r in m["mirrors"]) for m in report["monitors"]) or "none")
        if broadcast is not None:
            broadcast({"type": "monitors"})
        if on_change is not None:
            on_change()
    return changed


def view(computers: list[dict], connected: set[str]) -> dict:
    """Each KVM computer's monitors for the setup page. A computer's tray app
    is found by its name or an alias, like everywhere else."""
    with _lock:
        data = _load()
    by_peer = {p.lower(): (p, entry) for p, entry in data["computers"].items()}
    out = []
    for computer in computers:
        names = [computer["name"], *computer.get("aliases", [])]
        found = next((by_peer[n.lower()] for n in names if n.lower() in by_peer), None)
        peer, entry = found if found else (None, {})
        out.append({"computer": computer["name"], "peer": peer,
                    "online": peer is not None and peer.lower() in {c.lower() for c in connected},
                    "source": entry.get("source"), "seen": entry.get("seen"),
                    "monitors": entry.get("monitors", [])})
    return {"computers": out, "known": data["known"]}
