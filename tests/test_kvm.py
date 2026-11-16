"""The KVM bar and API (app/kvm.py): the computers sharing the desk's
keyboard and mouse, the active one, and switching. The engine (the
headless Deskflow server) is a status file and a fake web UI here."""

from __future__ import annotations

import http.server
import json
import socket
import socketserver
import threading

import pytest
from fastapi.testclient import TestClient

from app import auth, kvm
from app.main import app

STATUS = {"active": "sagedeck", "server": "pi",
          "clients": [{"name": "sage", "width": 5360, "height": 1440},
                      {"name": "sage-dev", "width": 1920, "height": 1080},
                      {"name": "sagedeck", "width": 2560, "height": 1440}]}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAGI_KVM_DIR", str(tmp_path))
    (tmp_path / "status.json").write_text(json.dumps(STATUS))
    return tmp_path


def test_the_view_lists_connected_computers_and_never_the_host_itself():
    assert kvm.view_from_status(STATUS) == {
        "available": True, "active": "sagedeck", "computers": ["sage", "sage-dev", "sagedeck"]}
    # Right after a client drops, the server is briefly its own active screen.
    assert kvm.view_from_status({**STATUS, "active": "pi"})["active"] is None
    assert kvm.view_from_status(None) == {"available": False, "active": None, "computers": []}


def test_a_missing_or_broken_status_file_means_no_kvm(engine):
    (engine / "status.json").write_text("{not json")
    assert kvm.read_view()["available"] is False
    (engine / "status.json").unlink()
    assert kvm.read_view()["available"] is False


def test_switching_to_a_computer_that_isnt_connected_never_reaches_the_engine(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(kvm, "_post", lambda path, body: calls.append((path, body)))
    with pytest.raises(kvm.KvmError, match="isn't connected"):
        kvm.switch("homeserver")
    kvm.switch("sage-dev")
    assert calls == [("/api/switch", {"computer": "sage-dev"})]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")
def test_the_engine_is_asked_over_its_unix_socket(engine):
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers.get("X-Deskflow-Webui"), body))
            reply = {"ok": True} if body["computer"] != "sagedeck" else {"ok": False, "error": "sagedeck isn't connected"}
            data = json.dumps(reply).encode()
            self.send_response(200 if reply["ok"] else 400)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    class Server(socketserver.UnixStreamServer):
        def get_request(self):
            request, _ = super().get_request()
            return request, ("local", 0)

    server = Server(str(engine / "webui.sock"), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        kvm.switch("sage")
        with pytest.raises(kvm.KvmError, match="sagedeck isn't connected"):
            kvm.switch("sagedeck")
    finally:
        server.shutdown()
        server.server_close()
    assert seen[0] == ("/api/switch", "1", {"computer": "sage"})


def test_the_dashboard_bar_shows_the_computers_and_switches(engine, monkeypatch, session):
    switched = []
    monkeypatch.setattr(kvm, "_post", lambda path, body: switched.append(body["computer"]))
    client = TestClient(app)

    page = client.get("/").text
    assert 'class="kvm-computer active"' in page and ">sagedeck</button>" in page and ">pi<" not in page

    assert client.post("/kvm/switch", data={"computer": "sage"}).status_code == 200
    assert switched == ["sage"]
    assert "isn&#39;t connected" in client.post("/kvm/switch", data={"computer": "ghost"}).text


def test_the_api_reads_and_switches(engine, monkeypatch, session):
    monkeypatch.setattr(kvm, "_post", lambda path, body: None)
    _, key = auth.create_api_key("test app")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {key}"

    assert client.get("/api/v1/kvm").json() == {
        "available": True, "active": "sagedeck", "computers": ["sage", "sage-dev", "sagedeck"]}
    assert client.post("/api/v1/kvm/active", json={"computer": "sage"}).status_code == 200
    assert client.post("/api/v1/kvm/active", json={"computer": "ghost"}).status_code == 409
    assert TestClient(app).get("/api/v1/kvm").status_code == 401


def test_the_setup_page_only_passes_on_its_own_requests(engine):
    assert kvm.setup_request("GET", "/api/bindings", b"", None)[0] == 404
    assert kvm.setup_request("GET", "/../etc/passwd", b"", None)[0] == 404
    assert kvm.setup_request("POST", "/api/layout", b"{}", None)[0] == 403      # no page header
    assert kvm.setup_request("POST", "/index.html", b"{}", "1")[0] == 403      # not an API path
    assert kvm.setup_request("POST", "/api/layout", b"x" * (kvm.SETUP_MAX_BODY + 1), "1")[0] == 400
    # Nothing listening: a clear 502, not an exception.
    status, _, body = kvm.setup_request("GET", "/api/state", b"", None)
    assert status == 502 and b"isn't reachable" in body


def test_the_setup_page_is_served_under_bragi_and_behind_its_login(engine, monkeypatch, session):
    assert 'href="/kvm/setup/"' in TestClient(app).get("/").text  # linked from Settings
    seen = []

    def fake(method, path, body, guard):
        seen.append((method, path, body, guard))
        return 200, "text/html; charset=utf-8", b"<html>setup</html>"

    monkeypatch.setattr(kvm, "setup_request", fake)
    client = TestClient(app)

    assert client.get("/kvm/setup", follow_redirects=False).headers["location"] == "/kvm/setup/"
    assert client.get("/kvm/setup/").text == "<html>setup</html>"
    client.post("/kvm/setup/api/layout", content=b'{"cells": []}', headers={"X-Deskflow-Webui": "1"})
    assert seen == [("GET", "/", b"", None), ("POST", "/api/layout", b'{"cells": []}', "1")]

    auth.set_password("hunter22")
    locked = TestClient(app).get("/kvm/setup/", follow_redirects=False)
    assert locked.status_code != 200


def test_a_tray_app_gets_the_kvm_on_connect_and_can_switch(engine, monkeypatch, session):
    switched = []

    def fake_switch(computer):
        if computer == "ghost":
            raise kvm.KvmError("ghost isn't connected.")
        switched.append(computer)

    monkeypatch.setattr(kvm, "switch", fake_switch)
    with TestClient(app).websocket_connect("/ws/peer/deck") as socket:
        assert socket.receive_json() == {"type": "kvm", "available": True, "active": "sagedeck",
                                         "computers": ["sage", "sage-dev", "sagedeck"]}
        socket.send_json({"type": "kvm_switch", "computer": "ghost"})
        assert socket.receive_json() == {"type": "kvm_error", "error": "ghost isn't connected."}
        socket.send_json({"type": "kvm_switch", "computer": "sage"})
        socket.send_json({"type": "kvm_switch", "computer": "ghost"})
        socket.receive_json()  # the second error shows the first switch has run
    assert switched == ["sage"]



def test_identify_asks_each_tray_app_to_show_its_computer(engine, monkeypatch, session):
    """Like Windows' "Identify": each connected tray app is told which KVM
    computer it is (by peer name or alias); the dashboard hears who wasn't
    there to show it."""
    from app import kvm_config
    monkeypatch.setattr(kvm_config, "read", lambda: {"computers": [
        {"name": "sage", "aliases": []}, {"name": "sagedeck", "aliases": ["deck"]},
        {"name": "sage-dev", "aliases": []}]})
    client = TestClient(app)
    with client.websocket_connect("/ws/peer/deck") as deck, client.websocket_connect("/ws/peer/laptop") as laptop:
        deck.receive_json(), laptop.receive_json()  # the "kvm" view on connect
        answer = client.post("/kvm/identify").json()
        assert deck.receive_json() == {"type": "kvm_identify", "computer": "sagedeck", "peer": "deck"}
        assert laptop.receive_json() == {"type": "kvm_identify", "computer": None, "peer": "laptop"}
    assert answer == {"shown": {"deck": "sagedeck", "laptop": None}, "missing": ["sage", "sage-dev"]}
