"""Connecting an app with a one-time code (app/pairing.py)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, pairing
from app.main import app


@pytest.fixture(autouse=True)
def fresh_codes(monkeypatch):
    monkeypatch.setattr(pairing, "_codes", {})
    monkeypatch.setattr(pairing, "_failures", 0)


def test_a_code_works_once_and_gives_a_working_key(session):
    code = pairing.new_code(now=0)
    assert len(code) == 9 and code[4] == "-"
    token = pairing.redeem(code.lower().replace("-", " "), "laptop", now=10)
    assert auth.check_api_key(token)["name"] == "laptop (connected with a code)"
    with pytest.raises(pairing.PairingError):
        pairing.redeem(code, "laptop", now=11)


def test_a_code_runs_out(session):
    code = pairing.new_code(now=0)
    with pytest.raises(pairing.PairingError):
        pairing.redeem(code, "laptop", now=pairing.CODE_SECONDS + 1)


def test_guessing_throws_every_code_away(session):
    code = pairing.new_code(now=0)
    for _ in range(pairing.MAX_FAILURES):
        with pytest.raises(pairing.PairingError):
            pairing.redeem("AAAA-AAAA", "laptop", now=1)
    with pytest.raises(pairing.PairingError):
        pairing.redeem(code, "laptop", now=2)


def test_names_and_addresses_are_checked(session):
    code = pairing.new_code(now=0)
    with pytest.raises(pairing.PairingError):
        pairing.redeem(code, "Bad Name", now=1)
    assert pairing.server_url("https://bragi.example.ts.net") == "https://bragi.example.ts.net/"
    assert pairing.server_url("http://127.0.0.1:20080") == "http://127.0.0.1:20080/"
    for bad in ("javascript:alert(1)", "https://x.test/path", "https://u:p@x.test", "ftp://x.test"):
        with pytest.raises(pairing.PairingError):
            pairing.server_url(bad)


def test_the_link_carries_server_and_code():
    assert pairing.link("https://bragi.example.ts.net/", "K7F2-9QXM") == \
        "bragi://connect?server=https%3A%2F%2Fbragi.example.ts.net%2F&code=K7F2-9QXM"
    assert pairing.qr_svg("bragi://connect").lstrip().startswith("<svg")


def test_the_dashboard_shows_a_code_and_an_app_redeems_it(session):
    client = TestClient(app)
    page = client.post("/pair", data={"origin": "https://bragi.example.ts.net"}).text
    assert "<svg" in page and "bragi://connect?server=https%3A%2F%2Fbragi.example.ts.net%2F&amp;code=" in page
    code = next(iter(pairing._codes))
    reply = client.post("/api/peers/pair", json={"code": code, "name": "laptop"})
    assert reply.status_code == 200 and auth.check_api_key(reply.json()["api_key"])
    again = client.post("/api/peers/pair", json={"code": code, "name": "laptop"})
    assert again.status_code == 400 and "Make a new one" in again.json()["detail"]
    assert "form-error" in client.post("/pair", data={"origin": "nope"}).text
