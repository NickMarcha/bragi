"""Passkeys (app/passkeys.py): adding one in Security and signing in with
it. A small software authenticator below plays the part of Bitwarden: a
P-256 key that builds and signs WebAuthn data the way a real one does, so
the real verification code (py_webauthn) runs end to end."""

from __future__ import annotations

import base64
import hashlib
import json
import os

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app import auth
from app.main import app

HOST = "sagepi.example.ts.net"
ORIGIN = f"https://{HOST}"
SITE = {"Origin": ORIGIN, "Host": HOST}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Authenticator:
    """One discoverable credential, like a passkey in a password manager."""

    def __init__(self, rp_id: str = HOST):
        self.rp_id = rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = os.urandom(16)
        self.count = 0

    def _auth_data(self, flags: int, attested: bytes = b"") -> bytes:
        self.count += 1
        return hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + self.count.to_bytes(4, "big") + attested

    def _client_data(self, kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin}).encode()

    def create(self, options: dict, origin: str = ORIGIN) -> dict:
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
        attested = bytes(16) + len(self.id).to_bytes(2, "big") + self.id + cose
        auth_data = self._auth_data(0x45, attested)  # user present, verified, attested data
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64url(self.id), "rawId": b64url(self.id), "type": "public-key",
                "response": {"clientDataJSON": b64url(client_data), "attestationObject": b64url(attestation)},
                "clientExtensionResults": {}}

    def get(self, options: dict, origin: str = ORIGIN) -> dict:
        auth_data = self._auth_data(0x05)  # user present and verified
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64url(self.id), "rawId": b64url(self.id), "type": "public-key",
                "response": {"clientDataJSON": b64url(client_data), "authenticatorData": b64url(auth_data),
                             "signature": b64url(signature), "userHandle": b64url(auth.passkey_user_id())},
                "clientExtensionResults": {}}


def add_passkey(client: TestClient, device: Authenticator, name: str = "Bitwarden") -> dict:
    start = client.post("/auth/passkeys/options", headers=SITE).json()
    response = client.post("/auth/passkeys", headers=SITE, json={
        "token": start["token"], "name": name, "credential": device.create(start["options"])})
    assert response.status_code == 200, response.text
    return response.json()


def sign_in(client: TestClient, device: Authenticator, origin: str = ORIGIN):
    start = client.post("/auth/passkey-login/options", headers=SITE).json()
    return client.post("/auth/passkey-login", headers=SITE, json={
        "token": start["token"], "next": "/kvm", "credential": device.get(start["options"], origin)})


def test_a_passkey_added_in_security_signs_in_and_locks_out_everyone_else(session):
    device = Authenticator()
    added = add_passkey(TestClient(app), device)
    assert (added["name"], added["rp_id"]) == ("Bitwarden", HOST)
    assert auth.locked() and not auth.password_set()

    stranger = TestClient(app)
    assert stranger.get("/", follow_redirects=False).status_code == 401
    page = stranger.get("/login").text
    assert "Sign in with a passkey" in page and 'type="password"' not in page

    browser = TestClient(app)
    response = sign_in(browser, device)
    assert response.json() == {"next": "/kvm"}
    assert browser.get("/").status_code == 200
    assert auth.passkeys()[0]["last_used"] is not None


def test_a_passkey_from_another_site_or_an_unknown_one_is_refused(session):
    device = Authenticator()
    add_passkey(TestClient(app), device)

    assert sign_in(TestClient(app), device, origin="https://evil.example").status_code == 401
    assert sign_in(TestClient(app), Authenticator()).status_code == 401  # never added here


def test_a_challenge_is_used_once(session):
    device = Authenticator()
    add_passkey(TestClient(app), device)
    client = TestClient(app)
    start = client.post("/auth/passkey-login/options", headers=SITE).json()
    body = {"token": start["token"], "next": "/", "credential": device.get(start["options"])}
    assert client.post("/auth/passkey-login", headers=SITE, json=body).status_code == 200
    assert TestClient(app).post("/auth/passkey-login", headers=SITE, json=body).status_code == 401


def test_passkeys_need_this_sites_https_address(session):
    client = TestClient(app)
    assert client.post("/auth/passkeys/options", headers={"Origin": "https://evil.example", "Host": HOST}).status_code == 403
    plain = {"Origin": f"http://{HOST}", "Host": HOST}
    assert "HTTPS" in client.post("/auth/passkeys/options", headers=plain).json()["detail"]


def test_adding_a_passkey_needs_a_signed_in_browser(session):
    auth.set_password("hunter22")
    assert TestClient(app).post("/auth/passkeys/options", headers=SITE).status_code == 401


def test_removing_the_last_passkey_opens_bragi_again(session):
    added = add_passkey(TestClient(app), Authenticator())
    client = TestClient(app)
    sign_in(client, Authenticator())  # an unknown passkey doesn't sign in
    assert client.get("/", follow_redirects=False).status_code == 401
    assert auth.remove_passkey(added["id"])
    assert not auth.locked()
    assert TestClient(app).get("/").status_code == 200


def test_settings_list_passkeys_without_their_keys(session):
    device = Authenticator()
    add_passkey(TestClient(app), device)
    client = TestClient(app)
    assert client.get("/auth/settings").status_code == 401  # locked now
    sign_in(client, device)
    listed = client.get("/auth/settings").json()["passkeys"]
    assert len(listed) == 1 and "public_key" not in listed[0] and listed[0]["rp_id"] == HOST
