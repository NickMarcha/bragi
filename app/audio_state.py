"""Persisted (volume, balance) state for pannable nodes, keyed by PipeWire
node name.

PipeWire has no native volume+balance concept, only raw per-channel
volumes - so both the intended overall volume AND the intended left/right
mix have to be tracked together, here, as the single source of truth.

Critically, this pair is NEVER reconstructed by reading wpctl's volume
back from the node once balance is non-zero: wpctl reports only the FL
(first) channel's value, which is already skewed by whatever balance is
currently applied. Recomputing "current volume" from that reading before
applying a new balance/volume change compounds the skew every single time
either control is touched - confirmed live as the cause of a real bug
(volume quietly ratcheting down on every balance adjustment). So this
module is the only place "current volume" for a pannable node is ever
read from - not pipewire.get_volume_mute().

Keyed by node *name* (stable across restarts), not node id (reassigned
every time a module/device is reloaded).
"""

from __future__ import annotations

import threading

import yaml

from .peers import DATA_DIR

STATE_FILE = DATA_DIR / "balance.yaml"
_lock = threading.Lock()

_DEFAULT_VOLUME = 1.0
_DEFAULT_BALANCE = 0.0


def _load() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    return yaml.safe_load(STATE_FILE.read_text()) or {}


def _save(data: dict[str, dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(yaml.safe_dump(data, sort_keys=False))


def get_state(node_name: str) -> tuple[float, float]:
    """Returns (volume, balance), defaulting to (1.0, centered) the first
    time a node is ever touched. Also reads the older balance-only schema
    (a bare float per node, from before volume was tracked alongside it)
    so an existing data/balance.yaml from before this change doesn't 500
    the app on the first request after upgrading."""
    with _lock:
        entry = _load().get(node_name, {})
        if not isinstance(entry, dict):
            return _DEFAULT_VOLUME, float(entry)
        return entry.get("volume", _DEFAULT_VOLUME), entry.get("balance", _DEFAULT_BALANCE)


def set_state(node_name: str, volume: float, balance: float) -> None:
    with _lock:
        data = _load()
        data[node_name] = {"volume": volume, "balance": balance}
        _save(data)
