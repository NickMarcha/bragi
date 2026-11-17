"""Connecting an app with a code: the dashboard's "Connect a device".

The dashboard makes a one-time code and shows it as a QR code and a link,
bragi://connect?server=<this Bragi>&code=<code>. The apps fill in the
server from it (they find their own name and Tailscale address). The
desktop tray app also redeems the code at /api/peers/pair for an API key of
its own, which its volume window needs once the dashboard has a password,
so nobody has to copy a key by hand. The phone app needs no key.

A code works once and for ten minutes, and only lives in memory: a
restart forgets it. The redeem endpoint is in the open peer group (see
docs/api-and-access.md), so wrong guesses are counted, and too many in a
row throw away every open code.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from urllib.parse import quote, urlsplit

from . import auth

CODE_SECONDS = 600
MAX_FAILURES = 20
# No 0/O, 1/I/L: a code may be typed from the screen.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")

_lock = threading.Lock()
_codes: dict[str, float] = {}  # code -> expiry (monotonic)
_failures = 0


class PairingError(ValueError):
    pass


def _normal(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(code).upper())


def new_code(now: float | None = None) -> str:
    """A fresh one-time code, "K7F2-9QXM"."""
    now = time.monotonic() if now is None else now
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    with _lock:
        for old, expiry in list(_codes.items()):
            if expiry <= now:
                del _codes[old]
        _codes[raw] = now + CODE_SECONDS
    return f"{raw[:4]}-{raw[4:]}"


def redeem(code: str, name: str, now: float | None = None) -> str:
    """Uses up a code and returns a new API key for the app called name."""
    global _failures
    now = time.monotonic() if now is None else now
    if not _NAME.match(name or ""):
        raise PairingError("The app's name is lowercase letters, digits and dashes.")
    raw = _normal(code)
    with _lock:
        expiry = _codes.pop(raw, None)
        if expiry is None or expiry <= now:
            _failures += 1
            if _failures >= MAX_FAILURES:
                _codes.clear()
                _failures = 0
            raise PairingError("That code is wrong or has run out. Make a new one in the dashboard.")
        _failures = 0
    _record, token = auth.create_api_key(f"{name} (connected with a code)")
    return token


def server_url(origin: str) -> str:
    """The dashboard's address as the page sees it (location.origin), checked."""
    parts = urlsplit(str(origin).strip())
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.path not in ("", "/") \
            or parts.query or parts.fragment or parts.username or parts.password:
        raise PairingError("The dashboard's address doesn't look like one.")
    return f"{parts.scheme}://{parts.netloc}/"


def link(server: str, code: str) -> str:
    return f"bragi://connect?server={quote(server, safe='')}&code={quote(code, safe='')}"


def qr_svg(text: str) -> str:
    """The link as an inline SVG QR code."""
    import segno
    return segno.make(text, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")
