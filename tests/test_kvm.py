"""The KVM bar and API (app/kvm.py): the computers sharing the desk's
keyboard and mouse, the active one, and switching. The engine (the
headless Deskflow server) is a status file and a fake core socket here."""

from __future__ import annotations

import json
import socket
import socketserver
import threading
import time

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
    monkeypatch.setattr(kvm, "core_command", calls.append)
    with pytest.raises(kvm.KvmError, match="isn't connected"):
        kvm.switch("homeserver")
    kvm.switch("sage-dev")
    assert calls == ["switch=sage-dev"]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")
def test_the_engine_is_asked_over_its_core_socket(engine):
    seen = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            line = self.rfile.readline().decode().strip()
            seen.append(line)
            self.wfile.write(b"status=running\n")  # a broadcast to a GUI, skipped
            self.wfile.write(b"ok\n" if line != "switch=sagedeck" else b"error=sagedeck isn't connected\n")

    server = socketserver.UnixStreamServer(str(engine / "core.sock"), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        kvm.switch("sage")
        with pytest.raises(kvm.KvmError, match="sagedeck isn't connected"):
            kvm.switch("sagedeck")
        kvm.keystroke("Control+Shift+Alt+M", ["sage", "sagedeck"])
    finally:
        server.shutdown()
        server.server_close()
    assert seen == ["switch=sage", "switch=sagedeck", "keystroke=Control+Shift+Alt+M,sage:sagedeck"]


def _core(engine, reply):
    """A fake core socket; reply(line) is what it writes back (None: nothing)."""
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            answer = reply(self.rfile.readline().decode().strip())
            if answer is None:
                time.sleep(0.5)  # hung: reads, never answers
            else:
                self.wfile.write(answer.encode() + b"\n")

    server = socketserver.ThreadingUnixStreamServer(str(engine / "core.sock"), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")
def test_the_probe_asks_the_core_thread_and_switches_nothing(engine, monkeypatch):
    monkeypatch.setattr(kvm, "_TIMEOUT", 0.1)
    assert kvm.probe() is None  # no server: systemd's business
    seen = []
    answers = {"mode": "error=-bragi-probe- isn't connected"}
    server = _core(engine, lambda line: seen.append(line) or answers["mode"])
    try:
        assert kvm.probe() is True
        answers["mode"] = None
        assert kvm.probe() is False
    finally:
        server.shutdown()
        server.server_close()
    assert seen == ["switch=-bragi-probe-"] * 2


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")
def test_the_watchdog_kills_a_server_that_stopped_answering(engine, monkeypatch):
    """2026-11-22: deskflow-core's core thread hung; nobody could connect."""
    import asyncio
    monkeypatch.setattr(kvm, "_TIMEOUT", 0.05)
    answers = {"mode": "error=-bragi-probe- isn't connected"}
    server = _core(engine, lambda line: answers["mode"])
    kills = []

    async def run(seconds):
        task = asyncio.create_task(kvm.watchdog(lambda: kills.append(1), every=0.01, misses=3))
        await asyncio.sleep(seconds)
        task.cancel()

    try:
        asyncio.run(run(0.3))
        assert kills == []  # answering
        answers["mode"] = None
        asyncio.run(run(0.6))
        assert len(kills) >= 1
    finally:
        server.shutdown()
        server.server_close()

    # A stopped server (no socket) is left to systemd.
    kills.clear()
    (engine / "core.sock").unlink(missing_ok=True)
    asyncio.run(run(0.2))
    assert kills == []


def test_the_dashboard_bar_shows_the_computers_and_switches(engine, monkeypatch, session):
    switched = []
    monkeypatch.setattr(kvm, "core_command", lambda line: switched.append(line.split("=", 1)[1]))
    client = TestClient(app)

    page = client.get("/").text
    assert 'class="kvm-computer active"' in page and ">sagedeck</button>" in page and ">pi<" not in page

    assert client.post("/kvm/switch", data={"computer": "sage"}).status_code == 200
    assert switched == ["sage"]
    assert "isn&#39;t connected" in client.post("/kvm/switch", data={"computer": "ghost"}).text


def test_the_api_reads_and_switches(engine, monkeypatch, session):
    monkeypatch.setattr(kvm, "core_command", lambda line: None)
    _, key = auth.create_api_key("test app")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {key}"

    assert client.get("/api/v1/kvm").json() == {
        "available": True, "active": "sagedeck", "computers": ["sage", "sage-dev", "sagedeck"]}
    assert client.post("/api/v1/kvm/active", json={"computer": "sage"}).status_code == 200
    assert client.post("/api/v1/kvm/active", json={"computer": "ghost"}).status_code == 409
    assert TestClient(app).get("/api/v1/kvm").status_code == 401


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
