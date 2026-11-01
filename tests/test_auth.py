"""The web UI password, session cookies with rotating refresh tokens, and
what each group of paths lets through (see app/access.py)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth
from app.main import app


def login(client: TestClient, password: str = "hunter22") -> None:
    response = client.post("/auth/login", data={"password": password, "next": "/"}, follow_redirects=False)
    assert response.status_code == 303


def expire_access(client: TestClient) -> None:
    client.cookies.delete(auth.ACCESS_COOKIE)


def test_without_a_password_the_dashboard_stays_open(session):
    client = TestClient(app)
    assert client.get("/").status_code == 200
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "state"


def test_setting_a_password_keeps_this_browser_in_and_locks_out_others(session):
    me = TestClient(app)
    assert me.post("/auth/password", json={"new_password": "hunter22"}).status_code == 200
    assert me.get("/").status_code == 200

    stranger = TestClient(app)
    page = stranger.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login"
    assert stranger.get("/peers", headers={"hx-request": "true"}).headers["hx-redirect"] == "/login?next=%2Fpeers"
    assert stranger.get("/auth/settings").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with stranger.websocket_connect("/ws"):
            pass


def test_wrong_password_is_refused_and_right_one_logs_in(session):
    auth.set_password("hunter22")
    client = TestClient(app)
    assert client.post("/auth/login", data={"password": "nope"}).status_code == 401
    login(client)
    assert client.get("/auth/session").json() == {"password_set": True, "authenticated": True}


def test_login_redirect_only_goes_back_into_this_site(session):
    auth.set_password("hunter22")
    client = TestClient(app)
    response = client.post("/auth/login", data={"password": "hunter22", "next": "//evil.example/"},
                           follow_redirects=False)
    assert response.headers["location"] == "/"


def test_expired_access_cookie_is_renewed_from_the_refresh_token(session):
    auth.set_password("hunter22")
    client = TestClient(app)
    login(client)
    first_refresh = client.cookies[auth.REFRESH_COOKIE]
    expire_access(client)

    response = client.get("/auth/session")
    assert response.status_code == 200
    assert client.cookies[auth.ACCESS_COOKIE]
    assert client.cookies[auth.REFRESH_COOKIE] != first_refresh


def test_concurrent_requests_with_the_same_refresh_token_are_not_theft(session):
    """A page load fires several requests at once; only one gets to rotate."""
    auth.set_password("hunter22")
    client = TestClient(app)
    login(client)
    old_refresh = client.cookies[auth.REFRESH_COOKIE]
    assert auth.refresh_session(old_refresh) is not None  # the first request rotates

    racing = auth.refresh_session(old_refresh)
    assert racing is not None
    assert racing[2] is None  # no second new refresh token
    assert auth.session_count() == 1


def test_a_replayed_refresh_token_after_the_grace_period_ends_the_session(session, monkeypatch):
    auth.set_password("hunter22")
    client = TestClient(app)
    login(client)
    stolen = client.cookies[auth.REFRESH_COOKIE]
    assert auth.refresh_session(stolen) is not None
    monkeypatch.setattr(auth, "REUSE_GRACE", -1)

    assert auth.refresh_session(stolen) is None
    assert auth.session_count() == 0


def test_websocket_accepts_a_refresh_cookie_without_rotating_it(session):
    """A handshake cannot set cookies, so rotating there would strand the
    browser with a retired token."""
    auth.set_password("hunter22")
    client = TestClient(app)
    login(client)
    refresh = client.cookies[auth.REFRESH_COOKIE]
    expire_access(client)
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "state"
    assert auth.refresh_session(refresh)[2] is not None  # still the current one


def test_changing_the_password_logs_out_other_browsers(session):
    auth.set_password("hunter22")
    me, other = TestClient(app), TestClient(app)
    login(me)
    login(other)
    assert me.post("/auth/password", json={"current_password": "hunter22",
                                           "new_password": "correct horse"}).status_code == 200
    assert me.get("/auth/session").status_code == 200
    assert other.get("/auth/session").status_code == 401


def test_changing_the_password_needs_the_current_one(session):
    auth.set_password("hunter22")
    client = TestClient(app)
    login(client)
    response = client.post("/auth/password", json={"current_password": "wrong", "new_password": "abcdef"})
    assert response.status_code == 400
    assert auth.check_password("hunter22")


def test_removing_the_password_opens_the_dashboard_again(session):
    auth.set_password("hunter22")
    client = TestClient(app)
    login(client)
    assert client.request("DELETE", "/auth/password", json={"current_password": "hunter22"}).status_code == 200
    assert TestClient(app).get("/").status_code == 200


def test_logout_ends_the_session(session):
    auth.set_password("hunter22")
    client = TestClient(app)
    login(client)
    client.post("/auth/logout")
    assert auth.session_count() == 0
    assert client.get("/auth/session").status_code == 401


def test_cross_site_writes_with_a_session_cookie_are_refused(session):
    auth.set_password("hunter22")
    client = TestClient(app)
    login(client)
    response = client.post("/auth/api-keys", json={"name": "x"}, headers={"origin": "https://evil.example"})
    assert response.status_code == 403


def test_peer_apps_still_work_with_a_password_set(session):
    auth.set_password("hunter22")
    client = TestClient(app)
    response = client.post("/api/peers/register", json={
        "name": "fairphone", "tailscale_ip": "100.98.253.67", "capture_mode": "microphone"})
    assert response.status_code == 200
    with client.websocket_connect("/ws/peer/fairphone?client=android") as socket:
        assert socket.receive_json()["type"] == "peer_config"


def test_static_files_and_login_page_need_no_session(session):
    auth.set_password("hunter22")
    client = TestClient(app)
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/login").status_code == 200
