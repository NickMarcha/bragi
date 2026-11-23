"""The tray apps' peer socket over the home network (bragi#90).

The peer socket (/ws/peer/<name>) normally comes in through `tailscale
serve`; Bragi itself listens on 127.0.0.1 only. On 2026-11-21 the
internet was down, the hub had no Tailscale, and the audio went over to the
LAN (app/lan_fallback.py), but the peer socket stayed down: sage switched
to one display and the hub kept the two-monitor layout, so the top right
of the desk couldn't be reached.

This module serves only that socket on the LAN, as a second uvicorn server
on BRAGI_LAN_PEER_PORT (the packaged unit sets 8443; unset, nothing
listens). The path to /ws/peer has no login of its own (Tailscale was the
gate), so here it takes two things a stranger on the LAN can't have:

- TLS with a self-signed certificate made on first start. The tray app
  pins its SHA-256, which it got earlier over Tailscale.
- A token per computer name, HMAC(secret, name), sent with the "hub"
  message over Tailscale (peer_control). A tray app without one never
  tries the LAN.

Everything else on this port is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import re
import secrets
import ssl
import subprocess

from . import peers

logger = logging.getLogger("bragi.lan_peer")

HEADER = b"x-bragi-lan-token"
_PATH = re.compile(r"^/ws/peer/([a-z][a-z0-9-]{0,39})$")


def port() -> int | None:
    try:
        value = int(os.environ.get("BRAGI_LAN_PEER_PORT", "0"))
    except ValueError:
        return None
    return value if 0 < value < 65536 else None


def _dir():
    return peers.DATA_DIR / "lan-peer"


def _secret() -> bytes:
    path = _dir() / "secret"
    try:
        return bytes.fromhex(path.read_text().strip())
    except (OSError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value.hex())
        return value


def token(name: str) -> str:
    return hmac.new(_secret(), name.encode(), hashlib.sha256).hexdigest()


def _paths():
    return _dir() / "cert.pem", _dir() / "key.pem"


def ensure_certificate() -> bool:
    """Makes the self-signed certificate if there isn't one. False when it can't."""
    cert, key = _paths()
    if cert.exists() and key.exists():
        return True
    cert.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
             "-nodes", "-days", "36500", "-subj", "/CN=bragi-hub-lan", "-keyout", str(key), "-out", str(cert)],
            capture_output=True, check=True, timeout=30)
        os.chmod(key, 0o600)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("no certificate for the LAN peer socket: %s", exc)
        return False
    return True


def fingerprint() -> str | None:
    """SHA-256 of the certificate (DER), lowercase hex."""
    try:
        der = ssl.PEM_cert_to_DER_cert(_paths()[0].read_text())
    except (OSError, ValueError):
        return None
    return hashlib.sha256(der).hexdigest()


def info(name: str) -> dict | None:
    """What the "hub" message tells this computer about the LAN socket, or
    None when there's none to offer."""
    if (p := port()) is None or (sha := fingerprint()) is None:
        return None
    return {"port": p, "cert_sha256": sha, "token": token(name)}


class LanPeerApp:
    """Lets through only /ws/peer/<name> with that name's token."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            return
        match = _PATH.match(scope.get("path", ""))
        given = dict(scope.get("headers") or []).get(HEADER, b"").decode(errors="replace")
        if scope["type"] == "websocket" and match and hmac.compare_digest(given, token(match.group(1))):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"Not found\n"})


async def serve(app) -> None:
    """Runs as a lifespan task while BRAGI_LAN_PEER_PORT is set."""
    if (p := port()) is None:
        return
    if not await asyncio.to_thread(ensure_certificate):
        return
    import uvicorn

    class _Server(uvicorn.Server):
        # The main server handles SIGTERM; this one mustn't take it over.
        def install_signal_handlers(self) -> None:
            pass

        @contextlib.contextmanager
        def capture_signals(self):
            yield

    cert, key = _paths()
    config = uvicorn.Config(LanPeerApp(app), host="0.0.0.0", port=p, ssl_certfile=str(cert),
                            ssl_keyfile=str(key), lifespan="off", log_level="warning",
                            ws_ping_interval=20, ws_ping_timeout=20)
    server = _Server(config)
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise
    except (OSError, SystemExit) as exc:
        logger.warning("the LAN peer socket didn't start on port %d: %s", p, exc)
