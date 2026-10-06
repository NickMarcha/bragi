"""The order of devices on the dashboard, dragged into place by the user.

Kept on the Pi rather than in the browser so a phone and a desktop show
the same console, and every open tab follows a drag live (ws.py broadcasts
the new order). Ids are "headset:<card key>" and "peer:<name>". Devices the
saved order doesn't mention (new peers, a newly plugged headset) follow
it in their default order. Same yaml-under-DATA_DIR pattern as
viz_settings.py.
"""

from __future__ import annotations

import threading

import yaml

from .peers import DATA_DIR

LAYOUT_FILE = DATA_DIR / "layout.yaml"
_lock = threading.Lock()

# A browser sends this list; keep it to something a dashboard could hold.
_MAX_DEVICES = 200
_MAX_ID_LENGTH = 120


def valid_order(value: object) -> bool:
    return (isinstance(value, list) and len(value) <= _MAX_DEVICES
            and all(isinstance(i, str) and 0 < len(i) <= _MAX_ID_LENGTH for i in value))


def get_order() -> list[str]:
    with _lock:
        if not LAYOUT_FILE.exists():
            return []
        return (yaml.safe_load(LAYOUT_FILE.read_text()) or {}).get("order", [])


def set_order(order: list[str]) -> None:
    if not valid_order(order):
        raise ValueError("order must be a list of device ids")
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LAYOUT_FILE.write_text(yaml.safe_dump({"order": order}, sort_keys=False))


def positions(default_order: list[str]) -> dict[str, int]:
    """Each device's place: saved ones first, then the rest as they come."""
    saved = {device: i for i, device in enumerate(get_order())}
    ranked = sorted(default_order, key=lambda d: (saved.get(d, len(saved)), default_order.index(d)))
    return {device: i for i, device in enumerate(ranked)}
