"""The KVM: which computers share the desk's keyboard and mouse, which one
has them now, and switching between them.

The engine is the headless Deskflow server on this host. Everything that
knows that lives in this module: it reads the server's status file and
sends commands to deskflow-core's IPC socket (core.sock), both under
$XDG_RUNTIME_DIR/deskflow-headless/. The dashboard, the API and the tray
apps only see computers and the active one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger("bragi.kvm")

_POLL_SECONDS = 0.5
_TIMEOUT = 5


def engine_dir() -> Path:
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
        return view_from_status(json.loads((engine_dir() / "status.json").read_text()))
    except (OSError, ValueError):
        return view_from_status(None)


def core_command(line: str) -> None:
    """Sends one command to deskflow-core's IPC socket and waits for "ok" or
    "error=...". Other lines are the core's broadcasts to a GUI; skipped."""
    path = engine_dir() / "core.sock"
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        raise KvmError("The KVM isn't reachable: Unix sockets aren't available here")
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(_TIMEOUT)
        try:
            sock.connect(str(path))
            sock.sendall(line.encode() + b"\n")
            buffer = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    raise KvmError("The KVM closed the connection.")
                buffer += chunk
                while b"\n" in buffer:
                    reply, buffer = buffer.split(b"\n", 1)
                    text = reply.decode(errors="replace")
                    if text == "ok":
                        return
                    if text.startswith("error="):
                        raise KvmError(text[len("error="):])
        except OSError as exc:
            raise KvmError(f"The KVM isn't reachable: {exc}") from exc


def switch(computer: str) -> None:
    """Makes a connected computer the active one."""
    if computer not in read_view()["computers"]:
        raise KvmError(f"{computer} isn't connected.")
    core_command(f"switch={computer}")


def keystroke(keys: str, computers: list[str] | None = None) -> None:
    """Presses and releases a key combination, like "Control+Shift+Alt+M",
    on the listed computers (all of them for ["*"]) or the active one."""
    if not keys or len(keys) > 100 or any(c in keys for c in ",()\n\r=") or \
            any(not c or len(c) > 64 or any(x in c for x in ":,\n\r") for c in (computers or [])):
        raise KvmError('Keys look like "Control+Shift+Alt+M"; computers are names.')
    core_command(f"keystroke={keys}" + (f",{':'.join(computers)}" if computers else ""))


async def supervise(broadcast) -> None:
    """Broadcasts {"type": "kvm", ...} whenever the computers or the active
    one change. Polls the status file's mtime, which is cheap."""
    last_mtime = None
    last_view = None
    path = engine_dir() / "status.json"
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
