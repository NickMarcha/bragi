"""The Controllers dialog pairs game controllers with the host over BlueZ
(app/bluetooth.py) and picks the computer they play on (app/gamepad.py).
The D-Bus side is replaced by a fake here."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app import bluetooth, gamepad, peer_control
from app.bluetooth import BluetoothError, Device, Manager
from app.main import app

PAD = Device("E4:17:D8:3B:34:01", "8BitDo Ultimate 3mode Xbox", "input-gaming", True, True, True)
NEW = Device("AA:BB:CC:DD:EE:01", "Pro Controller", "input-gaming", False, False, False, rssi=-50)
FAR = Device("AA:BB:CC:DD:EE:02", "Far Pad", "input-gaming", False, False, False, rssi=-90)
PHONE = Device("AA:BB:CC:DD:EE:03", "Phone", "phone", False, False, False, rssi=-40)


class FakeBackend:
    def __init__(self, powered=True):
        self.powered = powered
        self.discovering = False
        self.devices_ = [PAD, NEW, FAR, PHONE]
        self.calls = []
        self.fail: str | None = None

    def subscribe(self, on_change):
        self.on_change = on_change

    async def devices(self):
        return self.powered, self.discovering, list(self.devices_)

    async def set_powered(self):
        self.calls.append("power")
        self.powered = True

    async def start_discovery(self):
        if self.fail:
            raise BluetoothError(self.fail)
        self.calls.append("scan")
        self.discovering = True

    async def stop_discovery(self):
        self.calls.append("stop")
        self.discovering = False

    async def pair(self, address):
        self.calls.append(("pair", address))
        if self.fail:
            raise BluetoothError(self.fail)

    async def remove(self, address):
        self.calls.append(("remove", address))
        self.devices_ = [d for d in self.devices_ if d.address != address]


def run(coro):
    return asyncio.run(coro)


def test_the_view_lists_paired_devices_and_controllers_found_nearest_first():
    async def go():
        changes = []
        manager = Manager(FakeBackend(), lambda: changes.append(1))
        return await manager.refresh()

    view = run(go())
    assert view["paired"] == [PAD]
    assert view["found"] == [NEW, FAR]  # the phone isn't a controller
    assert view["scanning"] is False


def test_scanning_powers_the_adapter_and_stops_by_itself(monkeypatch):
    monkeypatch.setattr(bluetooth, "SCAN_SECONDS", 0)

    async def go():
        backend = FakeBackend(powered=False)
        manager = Manager(backend, lambda: None)
        await manager.scan()
        scanning = manager.view()["scanning"]
        await asyncio.sleep(0.01)
        return backend.calls, scanning, manager.view()["scanning"]

    calls, during, after = run(go())
    assert calls == ["power", "scan", "stop"]
    assert (during, after) == (True, False)


def test_pairing_stops_the_scan_first_and_reports_failures():
    async def go():
        backend = FakeBackend()
        manager = Manager(backend, lambda: None)
        await manager.pair(NEW.address)
        ok_calls = list(backend.calls)
        backend.fail = "Pairing failed. Put the controller in pairing mode again and retry."
        await manager.pair(NEW.address)
        return ok_calls, manager.error, manager.pairing

    ok_calls, error, pairing = run(go())
    assert ok_calls == ["stop", ("pair", NEW.address)]
    assert error.startswith("Pairing failed")
    assert pairing is None


def test_a_blocked_adapter_is_explained():
    assert "rfkill" in bluetooth._message(Exception("org.bluez.Error.Blocked: Blocked through rfkill"))


def test_the_dialog_pairs_forgets_and_picks_the_computer(session):
    backend = FakeBackend()
    bluetooth.manager = Manager(backend, lambda: None)
    gamepad.hub = gamepad.Hub(peer_control.send, peer_control.broadcast_desktop)
    client = TestClient(app)

    page = client.get("/controllers").text
    assert "8BitDo Ultimate 3mode Xbox" in page and "Pro Controller" in page and "Phone" not in page

    assert client.post("/controllers/pair", data={"address": NEW.address}).status_code == 200
    assert client.post("/controllers/pair", data={"address": "not-an-address"}).status_code == 422

    client.post("/controllers/forget", data={"address": PAD.address})
    assert ("remove", PAD.address) in backend.calls

    assert client.post("/controllers/target", data={"target": "deck"}).status_code == 200
    assert gamepad.target() == "deck"
    assert client.post("/controllers/target", data={"target": "fairphone"}).status_code == 422
    client.post("/controllers/target", data={"target": ""})
    assert gamepad.target() is None


def test_without_bluetooth_the_dialog_says_so(session):
    page = TestClient(app).get("/controllers").text
    assert "Bluetooth isn&#39;t running" in page  # HTML-escaped apostrophe
