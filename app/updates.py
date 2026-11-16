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

import asyncio
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

STATE_DIR = Path(os.environ.get("BRAGI_UPDATE_DIR", "/var/lib/bragi"))
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
# A request the updater hasn't picked up after this long means it isn't
# listening (bragi-update.path stopped or missing).
QUEUE_TIMEOUT = 90


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
    _queued(view)
    return view


def _queued(view: dict) -> None:
    """A request newer than the updater's last word: it's about to start
    (the path unit takes a moment), or it never will."""
    if view["state"] == "running":
        return
    path = STATE_DIR / "update-request"
    try:
        asked = path.read_text().strip()
        mtime = path.stat().st_mtime
    except OSError:
        return
    at = view["at"] if isinstance(view["at"], (int, float)) else 0
    if asked not in ("check", "upgrade") or mtime <= at:
        return
    view["action"] = asked
    if time.time() - mtime < QUEUE_TIMEOUT:
        view["state"], view["message"] = "running", "queued"
    else:
        view["state"] = "failed"
        view["message"] = "The updater didn't start. Is bragi-update.path running? (systemctl status bragi-update.path)"


async def watch(broadcast: Callable[[dict], None], every: float = 1.0) -> None:
    """Tells dashboards (over their websocket) whenever the section would
    look different: a request queued, the updater starting or done, a
    request nobody picked up. They reload the section on "updates"."""
    last = status()
    while True:
        await asyncio.sleep(every)
        now = await asyncio.to_thread(status)
        if now != last:
            last = now
            broadcast({"type": "updates", "state": now["state"]})


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
