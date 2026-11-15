"""EQ presets for the HyperX headset dialog (headset_hid.py): a built-in
set, plus presets saved from the dialog.

Saved presets live in data/headset_eq_presets.yaml, so every browser
sees the same list and it survives a redeploy. They aren't tied to one
headset: the bands and the gain encoding are the same on every dongle.

Which preset is active is worked out from the headset's current gains,
never stored. Every tab then agrees, and moving one slider away from a
preset makes it "Custom" without anything to keep in sync.

The built-in presets are data, in eq_presets_builtin.yaml next to this
file. docs/eq-presets.md has the basis and sources for each curve.
"""

from __future__ import annotations

import threading
from pathlib import Path

import yaml

from . import headset_hid, peers

BUILT_IN_FILE = Path(__file__).with_name("eq_presets_builtin.yaml")


def _load_built_in() -> tuple[dict, ...]:
    """Read once at import. A malformed file stops the app starting, which
    is the right time to find out."""
    presets = yaml.safe_load(BUILT_IN_FILE.read_text(encoding="utf-8"))
    return tuple({"name": str(p["name"]), "description": str(p["description"]),
                  "gains": headset_hid.valid_eq(p["gains"])} for p in presets)


BUILT_IN = _load_built_in()

FILE = peers.DATA_DIR / "headset_eq_presets.yaml"
MAX_NAME = 40
_lock = threading.Lock()


def _load() -> list[dict]:
    if not FILE.exists():
        return []
    return yaml.safe_load(FILE.read_text()) or []


def _save_all(presets: list[dict]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(yaml.safe_dump(presets, sort_keys=False))


def list_all() -> list[dict]:
    """Built-ins first, then saved presets in the order they were saved."""
    with _lock:
        saved = _load()
    return ([{**p, "builtin": True} for p in BUILT_IN]
            + [{"name": p["name"], "description": "", "gains": p["gains"], "builtin": False} for p in saved])


def save(name, gains) -> dict:
    """Saves gains under name, replacing a saved preset of the same name
    (any case). Built-in names are taken."""
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
        raise ValueError(f"A preset name is 1 to {MAX_NAME} characters")
    name = name.strip()
    if any(p["name"].casefold() == name.casefold() for p in BUILT_IN):
        raise ValueError(f"{name!r} is a built-in preset")
    gains = headset_hid.valid_eq(gains)
    with _lock:
        saved = [p for p in _load() if p["name"].casefold() != name.casefold()]
        saved.append({"name": name, "gains": gains})
        _save_all(saved)
    return {"name": name, "description": "", "gains": gains, "builtin": False}


def delete(name) -> None:
    if not isinstance(name, str):
        raise ValueError("No such preset")
    if any(p["name"].casefold() == name.casefold() for p in BUILT_IN):
        raise ValueError(f"{name!r} is built in and can't be deleted")
    with _lock:
        saved = _load()
        kept = [p for p in saved if p["name"].casefold() != name.casefold()]
        if len(kept) == len(saved):
            raise ValueError(f"No saved preset {name!r}")
        _save_all(kept)


def gains_for(name) -> list[float]:
    if isinstance(name, str):
        for preset in list_all():
            if preset["name"].casefold() == name.casefold():
                return preset["gains"]
    raise ValueError(f"No preset {name!r}")


def matching(gains: list[float] | None) -> str | None:
    """The name of the first preset with exactly these gains, or None."""
    if gains is None:
        return None
    return next((p["name"] for p in list_all() if [float(g) for g in p["gains"]] == [float(g) for g in gains]), None)
