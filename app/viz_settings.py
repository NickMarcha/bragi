"""Persisted global visualizer settings - currently just an on/off switch.

Deliberately separate from audio_state.py (per-node volume/balance): this is
a single global toggle, not keyed by node, and it gates whether
level_meter.py runs any capture at all - a real resource/privacy control
(continuous mic/speaker capture costs Pi resources and taps live audio),
not just a display preference. Same yaml-under-DATA_DIR pattern as
audio_state.py, so it survives container recreate/redeploy the same way.
"""

from __future__ import annotations

import threading

import yaml

from .peers import DATA_DIR

SETTINGS_FILE = DATA_DIR / "viz_settings.yaml"
_lock = threading.Lock()

_DEFAULT_ENABLED = True


def _load() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    return yaml.safe_load(SETTINGS_FILE.read_text()) or {}


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(yaml.safe_dump(data, sort_keys=False))


def get_enabled() -> bool:
    with _lock:
        return _load().get("enabled", _DEFAULT_ENABLED)


def set_enabled(enabled: bool) -> None:
    with _lock:
        data = _load()
        data["enabled"] = enabled
        _save(data)
