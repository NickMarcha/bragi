"""The macro button: the headset's gesture reports, the saved actions, the
HTTP requests they send, and the dialog's WebSocket actions.

Requests go to small servers started here, on 127.0.0.1 or a unix socket
in tmp_path, never to anything real.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app import headset_hid, macro_button, mic_monitor, ws
from app.main import app

from .fake_hyperx import SERIAL, FakeDongle
from .fake_pipewire import HEADSET_CARD_ID


class _Handler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.server.requests.append({
            "method": self.command, "path": self.path,
            "headers": dict(self.headers.items()), "body": self.rfile.read(length).decode(),
        })
        if self.server.hang.is_set():
            self.server.release.wait(5)
        self.send_response(self.server.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def log_message(self, *args) -> None:
        pass


def _serve(server):
    server.requests = []
    server.status = 200
    server.hang = threading.Event()
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _stop(server) -> None:
    server.release.set()
    server.shutdown()
    server.server_close()


@pytest.fixture
def tcp_server():
    server = _serve(ThreadingHTTPServer(("127.0.0.1", 0), _Handler))
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    _stop(server)


@pytest.fixture
def unix_server(tmp_path):
    if not hasattr(socketserver, "UnixStreamServer"):
        pytest.skip("no unix sockets here")

    class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    path = tmp_path / "app.sock"
    server = _serve(UnixHTTPServer(str(path), _Handler))
    server.path = str(path)
    yield server
    _stop(server)


def http_action(**fields) -> dict:
    return {"type": "http", "method": "POST", "target": "url", "url": "", "socket": "", "path": "/",
            "headers": {}, "body": "", **fields}


# --- the headset's reports ---


def test_each_gesture_reaches_on_button_once_and_the_rest_is_ignored():
    pressed, changed = [], []
    dongle = FakeDongle()
    hs = headset_hid.attach(SERIAL, dongle.write, changed.append, lambda serial, g: pressed.append((serial, g)))
    dongle.deliver = hs.handle_report

    for code in (0x08, 0x10, 0x20, 0x01, 0x02):
        dongle.press(code)

    assert pressed == [(SERIAL, "single"), (SERIAL, "double"), (SERIAL, "triple")]
    assert changed == []


def test_a_failing_button_handler_doesnt_break_the_reader():
    def boom(serial, gesture):
        raise RuntimeError("boom")

    dongle = FakeDongle()
    hs = headset_hid.attach(SERIAL, dongle.write, lambda serial: None, boom)
    dongle.deliver = hs.handle_report

    dongle.press(0x08)
    dongle.notify(0x01, 9)

    assert hs.view()["battery"] == 9


# --- saved actions ---


def test_nothing_is_the_default():
    assert macro_button.actions_for(SERIAL) == {g: {"type": "none"} for g in ("single", "double", "triple")}


async def test_an_action_is_saved_per_headset_and_read_back_from_disk():
    action = http_action(target="socket", socket="/run/user/1000/app.sock", path="/api/x",
                         headers={"X-Thing": "1"}, body='{"a": 1}', url="http://kept/")
    await macro_button.set_action(SERIAL, "double", action)

    assert macro_button.actions_for(SERIAL)["double"] == action
    assert macro_button.actions_for("other")["double"] == {"type": "none"}
    assert "app.sock" in macro_button.SETTINGS_FILE.read_text()


@pytest.mark.parametrize("action", [
    http_action(url="ftp://host/x"),
    http_action(url="http:///nohost"),
    http_action(url="http://h/", body="{not json"),
    http_action(url="http://h/", headers={"Bad Name": "1"}),
    http_action(url="http://h/", headers={"X": "a\r\nInjected: 1"}),
    http_action(url="http://h/", headers={"Content-Length": "9"}),
    http_action(url="http://h/", method="TRACE"),
    http_action(target="socket", socket="relative.sock", path="/x"),
    http_action(target="socket", socket="/a.sock", path="no-slash"),
    {"type": "shell", "command": "rm -rf /"},
    "nothing",
])
def test_an_action_that_couldnt_run_is_refused(action):
    with pytest.raises(ValueError):
        macro_button.valid_action(action)


async def test_an_unknown_gesture_is_refused():
    with pytest.raises(ValueError):
        await macro_button.set_action(SERIAL, "quadruple", {"type": "none"})


# --- running them ---


async def test_a_request_goes_to_the_url_with_its_headers_and_body(tcp_server):
    action = http_action(method="PUT", url=f"{tcp_server.url}/hook?x=1", headers={"X-Token": "abc"},
                         body='{"on": true}')

    result = await macro_button.press(SERIAL, "single", action)

    assert result["ok"] is True and result["text"] == "HTTP 200 OK"
    [request] = tcp_server.requests
    assert (request["method"], request["path"], request["body"]) == ("PUT", "/hook?x=1", '{"on": true}')
    assert request["headers"]["X-Token"] == "abc"
    assert request["headers"]["Content-Type"] == "application/json"
    assert macro_button.view(SERIAL)["single"]["result"] == result


async def test_a_request_goes_to_a_unix_socket(unix_server):
    # Shaped like the first real use in docs/deployment.md.
    body = json.dumps({"keys": "Control+Shift+Alt+M", "computers": ["a", "b"]})
    await macro_button.set_action(SERIAL, "double", http_action(
        target="socket", socket=unix_server.path, path="/api/keystroke",
        headers={"X-Example": "1"}, body=body))

    result = await macro_button.press(SERIAL, "double")

    assert result["ok"] is True
    [request] = unix_server.requests
    assert (request["method"], request["path"]) == ("POST", "/api/keystroke")
    assert json.loads(request["body"])["keys"] == "Control+Shift+Alt+M"
    assert request["headers"]["X-Example"] == "1"


async def test_an_error_status_is_a_failure(tcp_server):
    tcp_server.status = 403

    result = await macro_button.press(SERIAL, "single", http_action(url=tcp_server.url))

    assert result["ok"] is False and result["text"] == "HTTP 403 Forbidden"


async def test_a_missing_socket_says_so(tmp_path):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no unix sockets here")
    missing = str(tmp_path / "gone.sock")

    result = await macro_button.press(SERIAL, "single", http_action(target="socket", socket=missing, path="/"))

    assert result == {"ok": False, "text": f"no socket at {missing}", "at": result["at"]}


async def test_a_silent_server_times_out_without_blocking_the_loop(tcp_server, monkeypatch):
    monkeypatch.setattr(macro_button, "TIMEOUT_SECONDS", 0.3)
    tcp_server.hang.set()
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.02)

    ticker = asyncio.create_task(tick())
    result = await macro_button.press(SERIAL, "single", http_action(url=tcp_server.url))
    ticker.cancel()

    assert result["ok"] is False and "0.3" in result["text"]
    assert ticks >= 5


async def test_a_press_while_the_last_one_runs_is_dropped(tcp_server):
    tcp_server.hang.set()
    action = http_action(url=tcp_server.url)

    first = asyncio.create_task(macro_button.press(SERIAL, "single", action))
    while not tcp_server.requests:
        await asyncio.sleep(0.01)
    assert await macro_button.press(SERIAL, "single", action) is None
    tcp_server.release.set()

    assert (await first)["ok"] is True
    assert len(tcp_server.requests) == 1


async def test_a_failure_is_logged_once_until_it_works_again(tcp_server, caplog):
    tcp_server.status = 500
    action = http_action(url=tcp_server.url)
    caplog.set_level(logging.INFO, logger="bragi.macro_button")

    for _ in range(3):
        await macro_button.press(SERIAL, "single", action)
    tcp_server.status = 204
    await macro_button.press(SERIAL, "single", action)

    messages = [r.getMessage() for r in caplog.records]
    assert sum("failed" in m for m in messages) == 1
    assert any("works again" in m for m in messages)


async def test_nothing_runs_for_nothing():
    assert await macro_button.press(SERIAL, "triple") is None
    assert macro_button.view(SERIAL)["triple"]["result"] is None


async def test_the_mic_monitoring_action_flips_bragis_monitor(session):
    await macro_button.set_action(SERIAL, "triple", {"type": "toggle_mic_monitor"})

    first = await macro_button.press(SERIAL, "triple")
    assert first["text"] == "mic monitoring on"
    assert mic_monitor.settings_for(HEADSET_CARD_ID)["enabled"] is True

    await macro_button.press(SERIAL, "triple")
    assert mic_monitor.settings_for(HEADSET_CARD_ID)["enabled"] is False


# --- the dashboard ---


@pytest.fixture
async def attached():
    loop = asyncio.get_running_loop()
    dongle = FakeDongle()
    hs = headset_hid.attach(SERIAL, dongle.write,
                            lambda serial: loop.create_task(ws.broadcast_headset_controls(serial)),
                            ws.on_macro_button)
    dongle.deliver = lambda report: loop.call_soon_threadsafe(hs.handle_report, report)
    return dongle


async def wait_for(broadcasts, check, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        for message in reversed(broadcasts.of_type("headset_controls")):
            if check(message["controls"]["macro"]):
                return message
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no matching broadcast in {broadcasts.of_type('headset_controls')}")
        await asyncio.sleep(0.01)


async def test_saving_from_the_dialog_reaches_every_tab(session, broadcasts, attached):
    action = http_action(url="http://127.0.0.1:9/hook")

    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_macro",
                           "value": {"gesture": "double", "action": action}})

    await wait_for(broadcasts, lambda m: m["double"]["action"] == action)


async def test_a_bad_action_from_the_dialog_saves_nothing(session, broadcasts, attached):
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_macro",
                           "value": {"gesture": "double", "action": http_action(url="nope")}})

    assert broadcasts.of_type("headset_controls") == []
    assert not macro_button.SETTINGS_FILE.exists()


async def test_test_runs_the_forms_action_without_saving_it(session, broadcasts, attached, tcp_server):
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "test_macro",
                           "value": {"gesture": "single", "action": http_action(url=tcp_server.url)}})

    message = await wait_for(broadcasts, lambda m: m["single"]["result"] is not None)
    assert message["controls"]["macro"]["single"]["result"]["ok"] is True
    assert message["controls"]["macro"]["single"]["action"] == {"type": "none"}
    assert len(tcp_server.requests) == 1


async def test_pressing_the_button_runs_the_saved_action_and_shows_the_result(session, broadcasts, attached,
                                                                              tcp_server):
    await macro_button.set_action(SERIAL, "double", http_action(url=f"{tcp_server.url}/pressed"))

    attached.press(0x10)

    message = await wait_for(broadcasts, lambda m: m["double"]["result"] is not None)
    assert message["key"] == HEADSET_CARD_ID
    assert [r["path"] for r in tcp_server.requests] == ["/pressed"]


async def test_the_dialog_shows_the_saved_action(session, attached):
    await macro_button.set_action(SERIAL, "double", http_action(
        target="socket", socket="/run/user/1000/app.sock", path="/api/x", headers={"X-A": "1", "X-B": "2"}))

    html = TestClient(app).get("/").text

    assert "Macro button" in html
    assert 'value="/run/user/1000/app.sock"' in html
    assert "X-A: 1\nX-B: 2</textarea>" in html
    assert '<option value="socket" selected>' in html
