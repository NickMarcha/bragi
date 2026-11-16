"""Updates for a packaged Bragi (and the Deskflow server next to it).

Bragi runs as a user and can't use apt. The package installs a root
updater (/usr/lib/bragi/bragi-update, bragi-update.service) that a path
unit starts when /var/lib/bragi/update-request changes: this module writes
"check" or "upgrade" there and reads the result from
/var/lib/bragi/update.json. A daily timer checks on its own.

Outside the package (Docker, a dev checkout) there's no updater, and the
dashboard says so instead of offering a button.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE_DIR = Path(os.environ.get("BRAGI_UPDATE_DIR", "/var/lib/bragi"))
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


class UpdateError(Exception):
    """Something the dashboard can show as it is."""


def running_version() -> str | None:
    """The package version this Bragi was installed as, or None outside one."""
    try:
        return VERSION_FILE.read_text().strip() or None
    except OSError:
        return None


def available() -> bool:
    return (STATE_DIR / "update-request").exists()


def status() -> dict:
    """What the dashboard shows: the versions, whether an update is waiting,
    and the updater's last run."""
    view = {"available": available(), "running": running_version(), "state": None, "action": None,
            "message": None, "at": None, "packages": {}, "newer": False}
    try:
        data = json.loads((STATE_DIR / "update.json").read_text())
    except (OSError, ValueError):
        return view
    if isinstance(data, dict):
        for key in ("state", "action", "message", "at"):
            view[key] = data.get(key)
        packages = data.get("packages") if isinstance(data.get("packages"), dict) else {}
        view["packages"] = {name: {"installed": p.get("installed") or None, "candidate": p.get("candidate") or None}
                            for name, p in packages.items() if isinstance(p, dict)}
    view["newer"] = any(p["installed"] and p["candidate"] and p["installed"] != p["candidate"]
                         for p in view["packages"].values())
    return view


def request(action: str) -> None:
    """Asks the root updater to "check" or "upgrade"."""
    if action not in ("check", "upgrade"):
        raise UpdateError("Unknown update action.")
    if not available():
        raise UpdateError("Updates are only available when Bragi is installed as a package.")
    if status().get("state") == "running":
        raise UpdateError("The updater is already running.")
    try:
        with open(STATE_DIR / "update-request", "w") as f:
            f.write(action + "\n")
    except OSError as exc:
        raise UpdateError(f"Can't reach the updater: {exc}") from exc
