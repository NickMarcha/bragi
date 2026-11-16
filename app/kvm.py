"""The KVM: which computers share the desk's keyboard and mouse, which one
has them now, and switching between them.

The engine is the headless Deskflow server on this host. Everything that
knows that lives in this module: it reads the server's status file and
sends commands to deskflow-core's IPC socket (core.sock), both under
$XDG_RUNTIME_DIR/deskflow-headless/. A server too old to listen there is
reached through its web UI instead (webui.sock). The dashboard, the API
and the tray apps only see computers and the active one.
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


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path):
        super().__init__("localhost", timeout=_TIMEOUT)
        self._path = path

    def connect(self):
        family = getattr(socket, "AF_UNIX", None)
        if family is None:  # a dev machine without Unix sockets
            raise OSError("Unix sockets aren't available here")
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.settimeout(_TIMEOUT)
        self.sock.connect(str(self._path))


def _post(path: str, body: dict) -> dict:
    conn = _UnixConnection(engine_dir() / "webui.sock")
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


# The engine's own setup page (computers, layout, hotkeys, input devices),
# served by Bragi at /kvm/setup/ so it sits behind Bragi's login instead of a
# port of its own. Only these paths are passed on.
_SETUP_GET = {"/", "/index.html", "/api/state"}
_SETUP_POST_PREFIX = "/api/"
SETUP_MAX_BODY = 256 * 1024


def setup_request(method: str, path: str, body: bytes, guard: str | None) -> tuple[int, str, bytes]:
    """Forwards one request from the setup page to the engine's web UI and
    returns (status, content type, body). A POST must carry the page's own
    X-Deskflow-Webui header, which a cross-site form can't send."""
    if method == "GET" and path not in _SETUP_GET:
        return 404, "application/json", b'{"ok": false, "error": "not found"}'
    if method == "POST" and (not path.startswith(_SETUP_POST_PREFIX) or guard != "1"):
        return 403, "application/json", b'{"ok": false, "error": "not allowed"}'
    if method not in ("GET", "POST") or len(body) > SETUP_MAX_BODY:
        return 400, "application/json", b'{"ok": false, "error": "bad request"}'
    conn = _UnixConnection(engine_dir() / "webui.sock")
    try:
        headers = {"X-Deskflow-Webui": "1"} if method == "POST" else {}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body if method == "POST" else None, headers)
        response = conn.getresponse()
        return response.status, response.getheader("Content-Type", "application/octet-stream"), response.read()
    except OSError as exc:
        message = json.dumps({"ok": False, "error": f"The KVM isn't reachable: {exc}"}).encode()
        return 502, "application/json", message
    finally:
        conn.close()


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


def _uses_core_socket() -> bool:
    return (engine_dir() / "core.sock").exists()


def switch(computer: str) -> None:
    """Makes a connected computer the active one."""
    if computer not in read_view()["computers"]:
        raise KvmError(f"{computer} isn't connected.")
    if _uses_core_socket():
        core_command(f"switch={computer}")
    else:
        _post("/api/switch", {"computer": computer})


def keystroke(keys: str, computers: list[str] | None = None) -> None:
    """Presses and releases a key combination, like "Control+Shift+Alt+M",
    on the listed computers (all of them for ["*"]) or the active one."""
    if not keys or len(keys) > 100 or any(c in keys for c in ",()\n\r=") or \
            any(not c or len(c) > 64 or any(x in c for x in ":,\n\r") for c in (computers or [])):
        raise KvmError('Keys look like "Control+Shift+Alt+M"; computers are names.')
    if _uses_core_socket():
        core_command(f"keystroke={keys}" + (f",{':'.join(computers)}" if computers else ""))
    else:
        _post("/api/keystroke", {"keys": keys, "computers": computers or []})


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
