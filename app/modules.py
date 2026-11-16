"""Which of Bragi's feature modules can run on this box.

Bragi is a core (device inventory, actions, settings, API, UI, sign-in)
plus modules that each need an engine on the box:

- audio: PipeWire (the mixer, peers, headsets' audio)
- kvm: the headless Deskflow server (computers, switching, KVM setup)
- controllers: Linux input devices (game controllers passed to computers)
- bluetooth: BlueZ on the system D-Bus (pairing)

A module whose engine isn't there is off: the API says so and the UI
leaves it out. Checked from the filesystem, so it's cheap; cached briefly
because the dashboard asks on every page.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import kvm

MODULES = ("audio", "kvm", "controllers", "bluetooth")
_CACHE_SECONDS = 5
_cache: tuple[float, dict[str, bool]] | None = None


def _runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{getattr(os, 'getuid', lambda: 1000)()}")


def detect() -> dict[str, bool]:
    engine = kvm.engine_dir()
    return {
        "audio": (_runtime_dir() / "pipewire-0").exists(),
        "kvm": (engine / "status.json").exists() or (engine / "core.sock").exists(),
        "controllers": Path("/proc/bus/input/devices").exists(),
        "bluetooth": Path(os.environ.get("BRAGI_DBUS_SOCKET", "/run/dbus/system_bus_socket")).exists(),
    }


def enabled() -> dict[str, bool]:
    global _cache
    now = time.monotonic()
    if _cache is None or now - _cache[0] > _CACHE_SECONDS:
        _cache = (now, detect())
    return dict(_cache[1])


def reset_cache() -> None:
    global _cache
    _cache = None
