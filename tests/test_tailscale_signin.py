"""Signing in by Tailscale account (app/auth.py, app/access.py). Through
`tailscale serve` every request carries Tailscale-User-Login, set by
Tailscale; a trusted account gets in without a password."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import auth
from app.main import app

ME = {"Tailscale-User-Login": "NickMarcha@github"}
STRANGER = {"Tailscale-User-Login": "someone@example.com"}


def test_trusting_your_own_account_locks_everyone_else_out(session):
    client = TestClient(app)
    assert client.post("/auth/tailscale/trust", headers=ME).json() == {"tailscale_logins": ["NickMarcha@github"]}

    assert auth.locked() and not auth.password_set()
    assert client.get("/", headers=ME).status_code == 200
    assert TestClient(app).get("/", headers=STRANGER, follow_redirects=False).status_code == 401
    assert TestClient(app).get("/", follow_redirects=False).status_code == 401  # not through Tailscale
    # A browser is sent to the login page instead.
    browser = {**STRANGER, "Accept": "text/html"}
    assert TestClient(app).get("/", headers=browser, follow_redirects=False).headers["location"] == "/login"


def test_the_login_page_explains_when_only_tailscale_accounts_get_in(session):
    auth.trust_login("NickMarcha@github")
    page = TestClient(app).get("/login", headers=STRANGER).text
    assert "only lets in trusted Tailscale accounts" in page and "someone@example.com" in page
    assert 'type="password"' not in page
    # A trusted account visiting /login goes straight on.
    assert TestClient(app).get("/login?next=/kvm", headers=ME, follow_redirects=False).headers["location"] == "/kvm"


def test_with_a_password_too_either_one_gets_in(session):
    auth.set_password("hunter22")
    auth.trust_login("NickMarcha@github")

    assert TestClient(app).get("/", headers=ME).status_code == 200
    stranger = TestClient(app)
    assert 'type="password"' in stranger.get("/login", headers=STRANGER).text
    stranger.post("/auth/login", data={"password": "hunter22", "next": "/"}, headers=STRANGER)
    assert stranger.get("/", headers=STRANGER).status_code == 200


def test_only_the_account_you_are_signed_in_with_can_be_added(session):
    client = TestClient(app)
    assert client.post("/auth/tailscale/trust").status_code == 400          # not through Tailscale
    client.post("/auth/tailscale/trust", headers=ME)
    assert auth.trusted_logins() == ["NickMarcha@github"]


def test_removing_the_last_account_opens_bragi_again(session):
    auth.trust_login("NickMarcha@github")
    client = TestClient(app)
    assert client.post("/auth/tailscale/untrust", json={"login": "NickMarcha@github"}, headers=ME).status_code == 200
    assert not auth.locked()
    assert TestClient(app).get("/").status_code == 200
    assert client.post("/auth/tailscale/untrust", json={"login": "nobody"}).status_code == 404


def test_a_trusted_account_still_cant_be_driven_cross_site(session):
    auth.trust_login("NickMarcha@github")
    response = TestClient(app).post("/auth/tailscale/untrust", json={"login": "NickMarcha@github"},
                                    headers={**ME, "Origin": "https://evil.example"})
    assert response.status_code == 403
    assert auth.trusted_logins() == ["NickMarcha@github"]


def test_the_dashboard_websocket_lets_a_trusted_account_in(session):
    auth.trust_login("NickMarcha@github")
    with TestClient(app).websocket_connect("/ws", headers=ME) as socket:
        assert socket.receive_json()["type"] == "state"


def test_settings_show_this_browsers_account_and_the_trusted_ones(session):
    auth.trust_login("NickMarcha@github")
    settings = TestClient(app).get("/auth/settings", headers=ME).json()
    assert (settings["tailscale_login"], settings["tailscale_logins"]) == ("NickMarcha@github", ["NickMarcha@github"])
