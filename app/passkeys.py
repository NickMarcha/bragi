"""Passkeys (WebAuthn) for the web UI: a way in that needs no password to
remember, kept in a password manager (Bitwarden, 1Password...) or the
device itself.

A passkey is bound to the site's host name (the "relying party ID"), so it
works at the address it was added on: for this box, the Tailscale name
(sagepi.<tailnet>.ts.net), which has a real HTTPS certificate. Adding one
needs a signed-in browser (Security dialog); signing in with one is public
(the login page). Each passkey is stored in auth.yaml as its credential id,
public key and signature counter; there is nothing secret on this side.

Challenges live in memory for two minutes, one per ceremony.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from urllib.parse import urlsplit

from . import auth

_CHALLENGE_TTL = 120
_challenges: dict[str, tuple[bytes, float, str]] = {}  # token -> (challenge, expiry, rp_id)
_lock = threading.Lock()


class PasskeyError(Exception):
    """Something the page can show as it is."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def site(host: str | None, scheme: str | None) -> tuple[str, str]:
    """(rp_id, origin) for the address the browser used. WebAuthn needs
    HTTPS, except on localhost."""
    host = (host or "").strip()
    name = urlsplit("//" + host).hostname or ""
    if not name:
        raise PasskeyError("Can't tell which address this page is on.")
    if scheme != "https" and name not in ("localhost", "127.0.0.1"):
        raise PasskeyError("Passkeys need the HTTPS address, e.g. https://" + name + "/.")
    return name, f"{scheme}://{host}"


def _remember(challenge: bytes, rp_id: str) -> str:
    token = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        for key in [k for k, (_, expiry, _) in _challenges.items() if expiry < now]:
            del _challenges[key]
        _challenges[token] = (challenge, now + _CHALLENGE_TTL, rp_id)
    return token


def _take(token: str, rp_id: str) -> bytes:
    with _lock:
        entry = _challenges.pop(token, None)
    if entry is None or entry[1] < time.time() or entry[2] != rp_id:
        raise PasskeyError("That took too long. Try again.")
    return entry[0]


def registration_options(rp_id: str) -> dict:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement, UserVerificationRequirement)
    existing = [PublicKeyCredentialDescriptor(id=_unb64url(p["id"])) for p in auth.passkeys() if p.get("rp_id") == rp_id]
    options = generate_registration_options(
        rp_id=rp_id, rp_name="Bragi", user_name="bragi", user_display_name="Bragi",
        user_id=auth.passkey_user_id(),
        # Discoverable, so signing in needs no user name first.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.PREFERRED),
        exclude_credentials=existing,
    )
    return {"token": _remember(options.challenge, rp_id), "options": json.loads(options_to_json(options))}


def register(rp_id: str, origin: str, token: str, credential: dict, name: str) -> dict:
    from webauthn import verify_registration_response
    challenge = _take(token, rp_id)
    try:
        verified = verify_registration_response(credential=credential, expected_challenge=challenge,
                                                expected_rp_id=rp_id, expected_origin=origin)
    except Exception as exc:
        raise PasskeyError(f"The passkey couldn't be checked: {exc}") from exc
    return auth.add_passkey({
        "id": _b64url(verified.credential_id), "public_key": _b64url(verified.credential_public_key),
        "sign_count": verified.sign_count, "rp_id": rp_id, "name": name.strip()[:60] or "Passkey",
    })


def login_options(rp_id: str) -> dict:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import UserVerificationRequirement
    options = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.PREFERRED)
    return {"token": _remember(options.challenge, rp_id), "options": json.loads(options_to_json(options))}


def login(rp_id: str, origin: str, token: str, credential: dict) -> None:
    from webauthn import verify_authentication_response
    challenge = _take(token, rp_id)
    raw_id = credential.get("rawId") or credential.get("id") or ""
    stored = next((p for p in auth.passkeys() if p["id"] == raw_id and p.get("rp_id") == rp_id), None)
    if stored is None:
        raise PasskeyError("That passkey isn't one Bragi knows. Add it in Security first.")
    try:
        verified = verify_authentication_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id, expected_origin=origin,
            credential_public_key=_unb64url(stored["public_key"]),
            credential_current_sign_count=stored.get("sign_count", 0))
    except Exception as exc:
        raise PasskeyError(f"The passkey couldn't be checked: {exc}") from exc
    auth.passkey_used(stored["id"], verified.new_sign_count)
