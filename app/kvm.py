"""The KVM: which computers share the desk's keyboard and mouse, which one
has them now, and switching between them.

The engine is the headless Deskflow server on this host. Everything that
knows that lives in this module: it reads the server's status file and
asks its web UI to switch, both under $XDG_RUNTIME_DIR/deskflow-headless/
(the container mounts /run/user/<uid>). The dashboard, the API and the
tray apps only see computers and the active one.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger("bragi.kvm")

_POLL_SECONDS = 0.5
_TIMEOUT = 5


def _engine_dir() -> Path:
    if override := os.environ.get("BRAGI_KVM_DIR"):
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{getattr(os, 'getuid', lambda: 1000)()}"
    return Path(runtime) / "deskflow-headless"


class KvmError(Exception):
    """Something the dashboard can show as it is."""


def view_from_status(status: dict | None) -> dict:
    """{available, active, computers} from the engine's status. The host
    itself (the headless server) is never offered as a computer."""
    if not status:
        return {"available": False, "active": None, "computers": []}
    server = status.get("server")
    computers = [c["name"] for c in status.get("clients", [])
                 if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"] != server]
    active = status.get("active")
    return {"available": True, "active": active if active in computers else None, "computers": computers}


def read_view() -> dict:
    try:
        return view_from_status(json.loads((_engine_dir() / "status.json").read_text()))
    except (OSError, ValueError):
        return view_from_status(None)


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path):
        super().__init__("localhost", timeout=_TIMEOUT)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(_TIMEOUT)
        self.sock.connect(str(self._path))


def _post(path: str, body: dict) -> dict:
    conn = _UnixConnection(_engine_dir() / "webui.sock")
    try:
        conn.request("POST", path, json.dumps(body),
                     {"Content-Type": "application/json", "X-Deskflow-Webui": "1"})
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
    except (OSError, ValueError) as exc:
        raise KvmError(f"The KVM isn't reachable: {exc}") from exc
    finally:
        conn.close()
    if response.status == 404:
        raise KvmError("The KVM server is too old to switch from Bragi. Update deskflow-headless.")
    if not data.get("ok"):
        raise KvmError(data.get("error") or f"The KVM refused ({response.status}).")
    return data


def switch(computer: str) -> None:
    """Makes a connected computer the active one."""
    if computer not in read_view()["computers"]:
        raise KvmError(f"{computer} isn't connected.")
    _post("/api/switch", {"computer": computer})


async def supervise(broadcast) -> None:
    """Broadcasts {"type": "kvm", ...} whenever the computers or the active
    one change. Polls the status file's mtime, which is cheap."""
    last_mtime = None
    last_view = None
    path = _engine_dir() / "status.json"
    while True:
        try:
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                mtime = None
            if mtime != last_mtime:
                last_mtime = mtime
                view = await asyncio.to_thread(read_view)
                if view != last_view:
                    last_view = view
                    broadcast({"type": "kvm", **view})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("kvm status poll failed")
        await asyncio.sleep(_POLL_SECONDS)
