"""The device inventory (app/hardware.py), the module detection
(app/modules.py) and the KVM's direct line to deskflow-core (app/kvm.py).
tests/data/pi_input_devices.txt is sagepi's /proc/bus/input/devices."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, hardware, kvm, modules
from app.bluetooth import Device
from app.main import app

PI = (Path(__file__).parent / "data" / "pi_input_devices.txt").read_text()
BT_PAD = '''I: Bus=0005 Vendor=2dc8 Product=901b Version=0001
N: Name="8BitDo Ultimate 3mode Xbox"
P: Phys=e4:5f:01:a7:88:03
U: Uniq=e4:17:d8:3b:34:01
H: Handlers=event25 js0
'''
ALL = {"audio": True, "kvm": True, "controllers": True, "bluetooth": True}
HEADSET = {"serial": "C1V5340J99", "name": "HyperX Cloud III S Wireless", "vendor": "03f0", "product": "06be",
           "battery": {"percent": 80, "charging": "no"}}
STATUS = {"hidpp": [{"device": "MX Master 3S", "battery": {"percent": 65, "state": "discharging"}}]}


def by_name(devices):
    return {d["name"]: d for d in devices}


def test_input_nodes_become_physical_devices_and_hdmi_is_left_out():
    devices = by_name(hardware.input_devices(PI + "\n" + BT_PAD))
    assert "vc4-hdmi-0" not in devices
    receiver = devices["Logitech USB Receiver"]
    assert (receiver["bus"], receiver["kinds"]) == ("usb", {"keyboard", "mouse"})
    assert devices["8BitDo Ultimate 3mode Xbox"]["kinds"] == {"controller"}
    assert devices["8BitDo Ultimate 3mode Xbox"]["id"] == "bt:e4:17:d8:3b:34:01"


def test_the_inventory_merges_headsets_receiver_devices_and_paired_bluetooth():
    paired = [Device("E4:17:D8:3B:34:01", "8BitDo Ultimate 3mode Xbox", "input-gaming", True, True, False),
              Device("AA:BB:CC:DD:EE:04", "K380", "input-keyboard", True, True, False)]
    devices = by_name(hardware.inventory(ALL, proc_text=PI, headsets=[HEADSET], engine_status=STATUS,
                                         bluetooth=paired, batteries=[]))

    headset = devices["HyperX Cloud III S Wireless"]
    assert (headset["kinds"], headset["owner"], headset["battery"]) == (
        ["headset"], "audio", {"percent": 80, "charging": False, "low": False})
    mouse = devices["MX Master 3S"]
    assert (mouse["bus"], mouse["owner"], mouse["battery"]) == (
        "receiver", "kvm", {"percent": 65, "charging": False, "low": False})
    assert devices["Logitech USB Receiver"]["owner"] == "kvm"
    # Paired but switched off: still listed, as not connected.
    assert (devices["8BitDo Ultimate 3mode Xbox"]["connected"], devices["8BitDo Ultimate 3mode Xbox"]["owner"]) == (False, "controllers")
    assert devices["K380"]["kinds"] == ["keyboard"]


def test_a_module_that_is_off_owns_nothing():
    devices = by_name(hardware.inventory({**ALL, "kvm": False}, proc_text=PI, headsets=[], engine_status={}, bluetooth=[],
                                         batteries=[]))
    assert devices["Logitech USB Receiver"]["owner"] is None


def test_modules_follow_their_engines(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("BRAGI_KVM_DIR", str(tmp_path / "deskflow-headless"))
    monkeypatch.setenv("BRAGI_DBUS_SOCKET", str(tmp_path / "dbus"))
    assert modules.detect()["audio"] is False and modules.detect()["kvm"] is False

    (tmp_path / "pipewire-0").touch()
    (tmp_path / "deskflow-headless").mkdir()
    (tmp_path / "deskflow-headless" / "status.json").write_text("{}")
    (tmp_path / "dbus").touch()
    detected = modules.detect()
    assert (detected["audio"], detected["kvm"], detected["bluetooth"]) == (True, True, True)


def test_the_dashboard_leaves_out_modules_that_are_off(monkeypatch, session):
    monkeypatch.setattr(modules, "enabled", lambda: {**ALL, "kvm": False, "bluetooth": False})
    page = TestClient(app).get("/").text
    assert 'id="kvm-bar"' not in page and 'id="bluetooth-toggle"' not in page and "/kvm/setup/" not in page


def test_the_api_lists_hardware_and_modules(monkeypatch, session):
    monkeypatch.setattr(hardware, "inventory", lambda enabled: [{"id": "x", "name": "Pad", "owner": "controllers"}])
    _, key = auth.create_api_key("test app")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {key}"
    assert client.get("/api/v1/hardware").json() == {"hardware": [{"id": "x", "name": "Pad", "owner": "controllers"}]}
    assert set(client.get("/api/v1").json()["modules"]) == {"audio", "kvm", "controllers", "bluetooth"}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAGI_KVM_DIR", str(tmp_path))
    (tmp_path / "status.json").write_text(json.dumps(
        {"active": "sage", "server": "pi", "clients": [{"name": "sage"}, {"name": "sage-dev"}]}))
    return tmp_path


def test_keystrokes_that_could_smuggle_a_second_command_are_refused(engine):
    for keys, computers in (("M\nstop", None), ("M,x", None), ("M", ["a:b"]), ("M", ["sage\n"]), ("", None)):
        with pytest.raises(kvm.KvmError):
            kvm.keystroke(keys, computers)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")
def test_commands_go_straight_to_deskflow_core(engine):
    lines = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(engine / "core.sock"))
    server.listen()

    def core():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                line = conn.makefile().readline().rstrip("\n")
                lines.append(line)
                conn.sendall(b"hello-gui\n")  # a broadcast to skip
                conn.sendall(b"error=sage-dev isn't connected\n" if "sage-dev" in line else b"ok\n")

    threading.Thread(target=core, daemon=True).start()
    try:
        kvm.switch("sage")
        kvm.keystroke("Control+Shift+Alt+M", ["sage"])
        with pytest.raises(kvm.KvmError, match="isn't connected"):
            kvm.switch("sage-dev")
    finally:
        server.close()
    assert lines == ["switch=sage", "keystroke=Control+Shift+Alt+M,sage", "switch=sage-dev"]


def test_a_keyboard_battery_is_remembered_between_reports_and_marked_low(session):
    keyboards = lambda devices: [d for d in devices if d["name"] == "Lenovo TrackPoint Keyboard II"]  # noqa: E731
    reported = [{"key": "1-1.1.2", "model": "Lenovo TrackPoint Keyboard II", "percent": 15, "charging": False}]
    first = keyboards(hardware.inventory(ALL, proc_text=PI, headsets=[], engine_status={}, bluetooth=[],
                                         batteries=reported, now=1000))
    assert [k["battery"] for k in first if k["id"].endswith("1.1.2")] == [
        {"percent": 15, "charging": False, "low": True, "seen": 1000}]

    # The kernel has nothing right now (ENODATA): the last value stays, with when it was seen.
    silent = [{"key": "1-1.1.2", "model": "Lenovo TrackPoint Keyboard II", "percent": None, "charging": None}]
    later = keyboards(hardware.inventory(ALL, proc_text=PI, headsets=[], engine_status={}, bluetooth=[],
                                         batteries=silent, now=5000))
    assert [k["battery"]["seen"] for k in later if k["id"].endswith("1.1.2")] == [1000]
    # The other TrackPoint connection has no battery.
    assert [k["battery"] for k in later if k["id"].endswith("1.1.3")] == [None]


@pytest.mark.skipif(os.name == "nt", reason="sysfs paths have colons, which Windows doesn't allow")
def test_kernel_batteries_are_read_from_sysfs(tmp_path):
    hid = tmp_path / "devices" / "platform" / "usb1" / "1-1" / "1-1.1" / "1-1.1.2" / "1-1.1.2:1.1" / "0003:17EF:60EE.0013"
    hid.mkdir(parents=True)
    ps = tmp_path / "class" / "power_supply" / "hid-0003:17EF:60EE.0013-battery"
    ps.mkdir(parents=True)
    for name, value in {"type": "Battery", "scope": "Device", "model_name": "Lenovo TrackPoint Keyboard II",
                        "capacity": "15", "status": "Discharging"}.items():
        (ps / name).write_text(value + "\n")
    try:
        (ps / "device").symlink_to(hid, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not allowed here")
    assert hardware.kernel_batteries(str(tmp_path)) == [
        {"key": "1-1.1.2", "model": "Lenovo TrackPoint Keyboard II", "percent": 15, "charging": False}]
