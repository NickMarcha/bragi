"""Actions for the headset's macro button.

The HyperX Cloud III S Wireless works out single, double and triple presses
itself and sends one input report per gesture (headset_hid.BUTTON_GESTURES).
Each gesture can have one action, saved per dongle serial in
data/macro_button.yaml:

- nothing (the default),
- one of the KVM actions (actions.py): switch to a computer, the next
  computer, or press keys on some computers (Discord's mute on all),
- an HTTP request, to an http(s) URL or to a unix socket on the host,
- turning Bragi's own mic monitoring (mic_monitor.py) on or off,
- muting or unmuting every mic the computers hear (microphone.py).

Bragi doesn't know what's on the other end of a request. A socket target
exists because services on the host often listen only on 127.0.0.1 or a
socket, which the container can't reach over TCP. The host's /run/user/UID
is mounted in the container (docker-compose.yml), so sockets there work.

Requests run in a worker thread with a short timeout, never on the event
loop or the hidraw reader. A gesture whose last request is still running
is dropped rather than queued. The last result of each gesture is kept in
memory for the dialog, and a failure is logged once until it changes or
the action works again.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

import yaml

from . import actions, headset_hid, headsets, mic_monitor, microphone, peers, pipewire

logger = logging.getLogger("bragi.macro_button")

SETTINGS_FILE = peers.DATA_DIR / "macro_button.yaml"
GESTURES = tuple(headset_hid.BUTTON_GESTURES.values())
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
TIMEOUT_SECONDS = 3.0
_MAX_TEXT = 2000
_MAX_BODY = 8000
_MAX_HEADERS = 20
_MAX_RESPONSE = 64 * 1024
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# http.client works these out from the body.
_RESERVED_HEADERS = {"content-length", "transfer-encoding"}

NOTHING = {"type": "none"}

_settings_lock = threading.Lock()
# (serial, gesture) -> the newest set_action() call written, so two saves
# that finish out of order in their threads can't undo the later one.
_written_seq: dict[tuple[str, str], int] = {}
_next_seq = 0

# serial -> gesture -> {"ok", "text", "at"}. Memory only: a restart starts
# with no results.
_results: dict[str, dict[str, dict]] = {}
_running: set[tuple[str, str]] = set()
_last_failure: dict[tuple[str, str], str] = {}


def _load() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    return yaml.safe_load(SETTINGS_FILE.read_text()) or {}


def actions_for(serial: str) -> dict[str, dict]:
    with _settings_lock:
        saved = _load().get(serial) or {}
    return {gesture: actions.migrate_webui_keystroke(saved.get(gesture) or dict(NOTHING)) for gesture in GESTURES}


def view(serial: str) -> dict:
    """What the dialog shows: each gesture's action and its last result."""
    results = _results.get(serial, {})
    return {gesture: {"action": action, "result": results.get(gesture)}
            for gesture, action in actions_for(serial).items()}


def _text(value, name: str, limit: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} is text up to {limit} characters")
    return value.strip()


def valid_action(action) -> dict:
    """The action as it's saved. Refuses anything that couldn't run. An HTTP
    action keeps both its URL and its socket fields, so switching between
    them in the dialog loses neither."""
    if not isinstance(action, dict):
        raise ValueError("an action is an object with a type")
    kind = action.get("type")
    if kind == "none":
        return dict(NOTHING)
    if kind in ("toggle_mic_monitor", "toggle_mic_mute"):
        return {"type": kind}
    if kind in actions.KVM_KINDS:
        return actions.valid_kvm(action)
    if kind != "http":
        raise ValueError(f"unknown action type {kind!r}")

    method = action.get("method", "POST")
    if method not in METHODS:
        raise ValueError(f"method is one of {', '.join(METHODS)}")
    target = action.get("target", "url")
    if target not in ("url", "socket"):
        raise ValueError("target is url or socket")
    url = _text(action.get("url"), "url")
    socket_path = _text(action.get("socket"), "socket")
    path = _text(action.get("path"), "path") or "/"
    body = _text(action.get("body"), "body", _MAX_BODY)

    if target == "url":
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the URL starts with http:// or https:// and names a host")
    else:
        if not socket_path.startswith("/"):
            raise ValueError("the socket is an absolute path, like /run/user/1000/app.sock")
        if not path.startswith("/") or any(c.isspace() for c in path):
            raise ValueError("the HTTP path starts with / and has no spaces")
    if body:
        try:
            json.loads(body)
        except ValueError:
            raise ValueError("the body isn't valid JSON") from None

    headers = action.get("headers") or {}
    if not isinstance(headers, dict) or len(headers) > _MAX_HEADERS:
        raise ValueError(f"headers are up to {_MAX_HEADERS} name: value pairs")
    clean_headers = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME.match(name) or name.lower() in _RESERVED_HEADERS:
            raise ValueError(f"{name!r} can't be a header name")
        value = _text(value, f"header {name}")
        if "\r" in value or "\n" in value:
            raise ValueError(f"header {name} has a line break in it")
        clean_headers[name] = value

    return {"type": "http", "method": method, "target": target, "url": url,
            "socket": socket_path, "path": path, "headers": clean_headers, "body": body}


def _save(serial: str, gesture: str, action: dict, seq: int) -> None:
    with _settings_lock:
        if seq < _written_seq.get((serial, gesture), -1):
            return
        _written_seq[(serial, gesture)] = seq
        data = _load()
        entry = data.get(serial) or {}
        entry[gesture] = action
        data[serial] = entry
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        pending = SETTINGS_FILE.with_suffix(".tmp")
        pending.write_text(yaml.safe_dump(data, sort_keys=False))
        pending.replace(SETTINGS_FILE)


def _gesture(gesture) -> str:
    if gesture not in GESTURES:
        raise ValueError(f"gesture is one of {', '.join(GESTURES)}")
    return gesture


async def set_action(serial: str, gesture, action) -> dict:
    global _next_seq
    gesture = _gesture(gesture)
    action = valid_action(action)
    _next_seq += 1
    await asyncio.to_thread(_save, serial, gesture, action, _next_seq)
    return action


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _connection(action: dict, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    if action["target"] == "socket":
        return _UnixConnection(action["socket"], timeout), action["path"]
    parts = urlsplit(action["url"])
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=timeout,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    return conn, path


def send_http(action: dict, timeout: float = TIMEOUT_SECONDS) -> tuple[bool, str]:
    """One request, blocking: run it in a thread. (ok, what happened)."""
    headers = {"User-Agent": "Bragi", **action["headers"]}
    body = action["body"].encode() if action["body"] else None
    if body is not None and not any(name.lower() == "content-type" for name in headers):
        headers["Content-Type"] = "application/json"
    try:
        conn, path = _connection(action, timeout)
        try:
            conn.request(action["method"], path, body=body, headers=headers)
            response = conn.getresponse()
            response.read(_MAX_RESPONSE)
        finally:
            conn.close()
    except TimeoutError:
        return False, f"no answer within {timeout:g} s"
    except FileNotFoundError:
        return False, f"no socket at {action['socket']}"
    except ConnectionRefusedError:
        return False, "connection refused"
    except ssl.SSLError as exc:
        return False, f"TLS error: {exc.reason or exc}"
    except (OSError, http.client.HTTPException) as exc:
        return False, str(exc) or type(exc).__name__
    text = f"HTTP {response.status}" + (f" {response.reason}" if response.reason else "")
    return 200 <= response.status < 300, text


def _card_for(serial: str) -> str | None:
    graph = pipewire.dump()
    return next((hs.key for hs in headsets.list_headsets(graph)
                 if headset_hid.serial_matches(hs.key, serial)), None)


async def _toggle_mic_monitor(serial: str) -> tuple[bool, str]:
    card_id = await asyncio.to_thread(_card_for, serial)
    if card_id is None:
        return False, "the headset's audio card isn't there"
    on = not mic_monitor.settings_for(card_id)["enabled"]
    await mic_monitor.set_enabled(card_id, on)
    return True, f"mic monitoring {'on' if on else 'off'}"


async def _toggle_mic_mute() -> tuple[bool, str]:
    """Mutes or unmutes every mic the computers hear (microphone.py): the
    real mute when the headset's own button switches between mics."""
    on = await asyncio.to_thread(microphone.toggle_muted)
    if microphone.broadcast is not None:
        microphone.broadcast({"type": "microphone"})
    return True, "all mics muted" if on else "mics live"


async def press(serial: str, gesture: str, action: dict | None = None) -> dict | None:
    """Runs a gesture's saved action, or `action` instead (the dialog's Test
    button, which tries what's in the form). Returns the result it
    recorded, or None when nothing ran: no action, or the last one is
    still going."""
    gesture = _gesture(gesture)
    action = actions_for(serial)[gesture] if action is None else valid_action(action)
    if action["type"] == "none":
        return None
    key = (serial, gesture)
    if key in _running:
        logger.debug("%s press on %s dropped, the last one is still running", gesture, serial)
        return None
    _running.add(key)
    try:
        if action["type"] == "http":
            ok, text = await asyncio.to_thread(send_http, action, TIMEOUT_SECONDS)
        elif action["type"] in actions.KVM_KINDS:
            ok, text = await actions.run_kvm(action)
        elif action["type"] == "toggle_mic_mute":
            ok, text = await _toggle_mic_mute()
        else:
            ok, text = await _toggle_mic_monitor(serial)
    except Exception as exc:
        logger.exception("%s press on %s failed", gesture, serial)
        ok, text = False, str(exc) or type(exc).__name__
    finally:
        _running.discard(key)

    if ok:
        if _last_failure.pop(key, None) is not None:
            logger.info("macro button %s on %s works again: %s", gesture, serial, text)
    elif _last_failure.get(key) != text:
        logger.warning("macro button %s on %s failed: %s", gesture, serial, text)
        _last_failure[key] = text
    result = {"ok": ok, "text": text, "at": time.time()}
    _results.setdefault(serial, {})[gesture] = result
    return result
