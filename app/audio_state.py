"""Persisted balance ("left/right mix") state, keyed by PipeWire node name.

PipeWire has no native balance concept, only raw per-channel volumes - so
applying a volume change needs to know what balance to keep applying, and
applying a balance change needs to know the current overall volume. Volume
is read live from wpctl each time; balance is intent only Bragi remembers,
so it lives here rather than on peers.Peer, which only models network
peers, not local hardware devices (see headsets.py).

Keyed by node *name* (stable across restarts), not node id (reassigned
every time a module/device is reloaded).
"""

from __future__ import annotations

import threading

import yaml

from .peers import DATA_DIR

BALANCE_FILE = DATA_DIR / "balance.yaml"
_lock = threading.Lock()


def _load() -> dict[str, float]:
    if not BALANCE_FILE.exists():
        return {}
    return yaml.safe_load(BALANCE_FILE.read_text()) or {}


def _save(data: dict[str, float]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BALANCE_FILE.write_text(yaml.safe_dump(data, sort_keys=False))


def get_balance(node_name: str) -> float:
    with _lock:
        return float(_load().get(node_name, 0.0))


def set_balance(node_name: str, balance: float) -> None:
    with _lock:
        data = _load()
        data[node_name] = balance
        _save(data)
