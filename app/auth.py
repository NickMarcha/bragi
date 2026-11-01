"""The web UI password, browser sessions, and API keys, stored in
data/auth.yaml.

No password set (the default, and how Bragi ran before this existed) means
the web UI is open to anyone who can reach it, which on sagepi is the
tailnet. Setting one from the dashboard's Security dialog turns on login.

A browser session is two cookies:

- an access token, signed with a server secret and valid for ACCESS_TTL.
  Checking it costs an HMAC, no file write.
- a refresh token, random and opaque, stored here only as a hash. When the
  access token has expired, the middleware trades the refresh token for a
  new pair (rotation). A refresh token that was already traded in is
  either a second request racing the first one (accepted for
  REUSE_GRACE seconds, see refresh_session) or a stolen copy, and then
  the whole session is ended.

Sessions have no absolute lifetime, only an idle one, so a browser that
comes back at least once every REFRESH_IDLE_TTL stays logged in for good.

API keys are for other apps. Each is shown once at creation and stored
here only as a hash.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import os
import secrets
import threading
import time

import yaml

from .peers import DATA_DIR

AUTH_FILE = DATA_DIR / "auth.yaml"

ACCESS_COOKIE = "bragi_access"
REFRESH_COOKIE = "bragi_refresh"

ACCESS_TTL = 15 * 60
# Chrome caps cookie lifetimes at 400 days. A rotation renews both cookies.
REFRESH_IDLE_TTL = 400 * 24 * 3600
# A page load sends several requests at once. If the access cookie has just
# expired, each of them carries the same refresh token; only the first gets
# to rotate it, and the rest must not read as theft.
REUSE_GRACE = 60
# last_used on an API key is written at most this often, not on every call.
API_KEY_TOUCH_SECONDS = 60
MIN_PASSWORD_LENGTH = 4

API_KEY_PREFIX = "bragi_"

_lock = threading.RLock()
_cache: tuple[str, int, dict] | None = None


class PasswordError(ValueError):
    pass


def _empty() -> dict:
    return {"password": None, "secret": None, "sessions": {}, "api_keys": []}


def _load() -> dict:
    """A copy of the store. Cached by mtime, because the middleware reads
    it on every request."""
    global _cache
    path = AUTH_FILE
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return _empty()
    if _cache is None or _cache[0] != str(path) or _cache[1] != mtime:
        data = yaml.safe_load(path.read_text()) or {}
        _cache = (str(path), mtime, {**_empty(), **data})
    return copy.deepcopy(_cache[2])


def _save(data: dict) -> None:
    global _cache
    path = AUTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    _cache = None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- password ---

# scrypt rather than PBKDF2: comparable strength at ~50ms on a Pi 4, where
# PBKDF2 at today's recommended iteration count takes about a second.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${_b64(salt)}${_b64(derived)}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(password.encode(), salt=_unb64(salt), dklen=32,
                                 n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, _unb64(expected))


def password_set() -> bool:
    return bool(_load().get("password"))


def check_password(password: str) -> bool:
    stored = _load().get("password")
    return bool(stored) and _verify_password(password, stored)


def set_password(new: str, current: str | None = None) -> None:
    """Sets or changes the password and ends every session, so a changed
    password logs out every other browser. The caller starts a fresh
    session for the browser that made the change."""
    if len(new) < MIN_PASSWORD_LENGTH:
        raise PasswordError(f"Use at least {MIN_PASSWORD_LENGTH} characters")
    with _lock:
        data = _load()
        if data.get("password") and not _verify_password(current or "", data["password"]):
            raise PasswordError("Current password is wrong")
        data["password"] = _hash_password(new)
        data["sessions"] = {}
        _save(data)


def remove_password(current: str) -> None:
    with _lock:
        data = _load()
        if data.get("password") and not _verify_password(current, data["password"]):
            raise PasswordError("Current password is wrong")
        data["password"] = None
        data["sessions"] = {}
        _save(data)


# --- browser sessions ---

def _secret(data: dict) -> bytes:
    if not data.get("secret"):
        data["secret"] = _b64(secrets.token_bytes(32))
    return _unb64(data["secret"])


def _sign(secret: bytes, payload: str) -> str:
    return _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())


def _access_token(secret: bytes, session_id: str, now: float) -> str:
    payload = f"{session_id}.{int(now) + ACCESS_TTL}"
    return f"{payload}.{_sign(secret, payload)}"


def start_session(user_agent: str = "") -> tuple[str, str]:
    """Returns (access token, refresh token) for a browser that just
    proved it knows the password."""
    now = time.time()
    refresh = secrets.token_urlsafe(32)
    session_id = secrets.token_urlsafe(9)
    with _lock:
        data = _load()
        secret = _secret(data)
        data["sessions"][session_id] = {
            "created": int(now), "last_used": int(now), "user_agent": user_agent[:160],
            "current": _digest(refresh), "previous": None, "rotated_at": None,
        }
        _save(data)
    return _access_token(secret, session_id, now), refresh


def check_access(token: str | None) -> str | None:
    """The session id an access token belongs to, or None if it is forged,
    expired, or its session has been ended."""
    if not token:
        return None
    try:
        session_id, expires, signature = token.split(".")
        expires_at = int(expires)
    except ValueError:
        return None
    data = _load()
    if not data.get("secret"):
        return None
    if not hmac.compare_digest(signature, _sign(_unb64(data["secret"]), f"{session_id}.{expires}")):
        return None
    if expires_at < time.time() or session_id not in data["sessions"]:
        return None
    return session_id


def _find_refresh(data: dict, refresh: str) -> tuple[str | None, str | None]:
    """(session id, "current" | "previous") for a refresh token."""
    digest = _digest(refresh)
    for session_id, session in data["sessions"].items():
        if hmac.compare_digest(session.get("current") or "", digest):
            return session_id, "current"
        if hmac.compare_digest(session.get("previous") or "", digest):
            return session_id, "previous"
    return None, None


def refresh_session(refresh: str | None) -> tuple[str, str, str | None] | None:
    """Trades a refresh token for (session id, access token, new refresh
    token). The new refresh token is None when this one was rotated a
    moment ago by a concurrent request: that request's response already
    carries the new cookie, so this one sets only an access token.

    Returns None, and ends the session, for a refresh token presented
    after its grace period: either someone else has a copy, or the
    browser lost a response. Either way, logging in again is the safe
    answer."""
    if not refresh:
        return None
    now = time.time()
    with _lock:
        data = _load()
        session_id, which = _find_refresh(data, refresh)
        if session_id is None:
            return None
        session = data["sessions"][session_id]
        if now - session["last_used"] > REFRESH_IDLE_TTL:
            del data["sessions"][session_id]
            _save(data)
            return None
        secret = _secret(data)
        if which == "previous":
            if session.get("rotated_at") and now - session["rotated_at"] <= REUSE_GRACE:
                return session_id, _access_token(secret, session_id, now), None
            del data["sessions"][session_id]
            _save(data)
            return None
        new_refresh = secrets.token_urlsafe(32)
        session.update(previous=session["current"], current=_digest(new_refresh),
                       rotated_at=int(now), last_used=int(now))
        _save(data)
    return session_id, _access_token(secret, session_id, now), new_refresh


def peek_refresh(refresh: str | None) -> str | None:
    """The session id for a still-valid refresh token, without rotating it.

    For WebSocket handshakes, which cannot set cookies: rotating there
    would leave the browser holding a token the server has already retired,
    and its next request would read as theft."""
    if not refresh:
        return None
    now = time.time()
    data = _load()
    session_id, which = _find_refresh(data, refresh)
    if session_id is None:
        return None
    session = data["sessions"][session_id]
    if now - session["last_used"] > REFRESH_IDLE_TTL:
        return None
    if which == "previous" and not (session.get("rotated_at") and now - session["rotated_at"] <= REUSE_GRACE):
        return None
    return session_id


def end_session(session_id: str | None) -> None:
    if not session_id:
        return
    with _lock:
        data = _load()
        if data["sessions"].pop(session_id, None) is not None:
            _save(data)


def end_other_sessions(keep: str | None) -> int:
    with _lock:
        data = _load()
        others = [s for s in data["sessions"] if s != keep]
        for session_id in others:
            del data["sessions"][session_id]
        if others:
            _save(data)
    return len(others)


def session_count() -> int:
    return len(_load()["sessions"])


# --- API keys ---

def _public_key(record: dict) -> dict:
    return {k: record.get(k) for k in ("id", "name", "prefix", "created", "last_used")}


def create_api_key(name: str) -> tuple[dict, str]:
    """Returns (the key's public record, the key itself). The key is not
    stored and cannot be shown again."""
    name = name.strip()
    if not name or len(name) > 60:
        raise ValueError("Give the connection a name of up to 60 characters")
    token = API_KEY_PREFIX + secrets.token_urlsafe(32)
    record = {
        "id": secrets.token_hex(4), "name": name, "prefix": token[:len(API_KEY_PREFIX) + 6],
        "hash": _digest(token), "created": int(time.time()), "last_used": None,
    }
    with _lock:
        data = _load()
        data["api_keys"].append(record)
        _save(data)
    return _public_key(record), token


def list_api_keys() -> list[dict]:
    return [_public_key(r) for r in _load()["api_keys"]]


def revoke_api_key(key_id: str) -> bool:
    with _lock:
        data = _load()
        kept = [r for r in data["api_keys"] if r["id"] != key_id]
        if len(kept) == len(data["api_keys"]):
            return False
        data["api_keys"] = kept
        _save(data)
    return True


def check_api_key(token: str | None) -> dict | None:
    if not token or not token.startswith(API_KEY_PREFIX):
        return None
    digest = _digest(token)
    data = _load()
    record = next((r for r in data["api_keys"] if hmac.compare_digest(r["hash"], digest)), None)
    if record is None:
        return None
    now = int(time.time())
    if record.get("last_used") is None or now - record["last_used"] >= API_KEY_TOUCH_SECONDS:
        with _lock:
            data = _load()
            for r in data["api_keys"]:
                if r["id"] == record["id"]:
                    r["last_used"] = now
            _save(data)
        record["last_used"] = now
    return _public_key(record)
