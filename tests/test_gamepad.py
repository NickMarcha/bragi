"""Game controllers on the Bragi host go to the computer that claimed them
(app/gamepad.py), as one Xbox layout whatever the kernel driver reports."""

from __future__ import annotations

import struct

import pytest

from fastapi.testclient import TestClient

from app import gamepad, peer_control
from app.gamepad import (ABS_BRAKE, ABS_GAS, ABS_HAT0X, ABS_RX, ABS_RZ, ABS_X, ABS_Y, ABS_Z, EV_ABS,
                         EV_KEY, EV_SYN, HID_GAMEPAD, XPAD, Controller, Hub, Normaliser, _Open)
from app.main import app

PROC = '''I: Bus=0003 Vendor=17ef Product=60ee Version=0111
N: Name="Lenovo TrackPoint Keyboard II"
U: Uniq=
H: Handlers=sysrq kbd leds event3

I: Bus=0005 Vendor=2dc8 Product=901b Version=0001
N: Name="8BitDo Ultimate 3mode Xbox"
U: Uniq=E4:17:D8:3B:34:01
H: Handlers=event25 js0

I: Bus=0003 Vendor=2dc8 Product=200f Version=0101
N: Name="8BitDo Ultimate 3-mode Controller for Xbox"
U: Uniq=
H: Handlers=kbd event26 js1
'''

# The 8BitDo over Bluetooth: every axis 0..255, d-pad -1..1.
BT_RANGES = {code: (0, 255) for code in (ABS_X, ABS_Y, ABS_Z, ABS_RZ, ABS_GAS, ABS_BRAKE)}
BT_RANGES.update({ABS_HAT0X: (-1, 1)})


def test_joysticks_are_found_and_keyboards_are_not():
    assert gamepad.list_controllers(PROC) == [
        Controller("e4:17:d8:3b:34:01", "8BitDo Ultimate 3mode Xbox", "event25"),
        Controller("8BitDo Ultimate 3-mode Controller for Xbox", "8BitDo Ultimate 3-mode Controller for Xbox", "event26"),
    ]


def test_the_layout_follows_the_driver():
    assert gamepad.layout_for({ABS_X, ABS_Y, ABS_RX, ABS_Z, ABS_RZ}) is XPAD
    assert gamepad.layout_for({ABS_X, ABS_Y, ABS_Z, ABS_RZ, ABS_GAS, ABS_BRAKE}) is HID_GAMEPAD


def test_bluetooth_input_becomes_the_xbox_layout():
    pad = Normaliser(HID_GAMEPAD, BT_RANGES)

    pad.feed(EV_KEY, 304, 1)        # A
    pad.feed(EV_KEY, 310, 1)        # LB
    pad.feed(EV_KEY, 306, 1)        # a back paddle: no Xbox 360 equivalent
    pad.feed(EV_ABS, ABS_Z, 255)    # right stick fully right
    pad.feed(EV_ABS, ABS_Y, 0)      # left stick fully up
    pad.feed(EV_ABS, ABS_BRAKE, 255)  # left trigger
    pad.feed(EV_ABS, ABS_HAT0X, -1)
    assert pad.feed(EV_SYN, 0, 0) is True

    assert pad.state() == {"buttons": ["a", "lb"], "lx": 0, "ly": -32768, "rx": 32767, "ry": 0,
                           "lt": 255, "rt": 0, "dx": -1, "dy": 0}


def test_only_a_report_after_a_change_is_sent():
    pad = Normaliser(HID_GAMEPAD, BT_RANGES)
    pad.feed(EV_ABS, ABS_X, 128)
    assert pad.feed(EV_SYN, 0, 0) is False  # centre is already the resting state
    pad.feed(EV_KEY, 304, 1)
    assert pad.feed(EV_SYN, 0, 0) is True
    pad.feed(EV_KEY, 304, 1)
    assert pad.feed(EV_SYN, 0, 0) is False


class Wire:
    def __init__(self):
        self.sent = []
        self.broadcasts = []

    def send(self, name, message, droppable):
        self.sent.append((name, message["type"], droppable))

    def broadcast(self, message):
        self.broadcasts.append(message)


def opened_hub(wire, hid=None, hid_file=None):
    hub = Hub(wire.send, wire.broadcast)
    pad = Normaliser(HID_GAMEPAD, BT_RANGES)
    hub.opened(_Open(Controller("pad1", "8BitDo", "event25"), pad, object(), hid, hid_file))
    return hub, pad


def test_switching_target_detaches_the_old_computer_and_hands_the_new_one_the_state():
    wire = Wire()
    hub, _ = opened_hub(wire)

    hub.set_target("deck")
    hub.set_target("sage")

    assert wire.sent == [("deck", "gamepad", False), ("deck", "gamepad_detach", False), ("sage", "gamepad", False)]
    assert wire.broadcasts[-1] == {"type": "gamepads", "target": "sage",
                                   "controllers": [{"id": "pad1", "name": "8BitDo"}]}
    assert gamepad.target() == "sage"  # saved for the next start


def test_input_goes_only_to_the_target_and_may_be_dropped():
    wire = Wire()
    hub, _ = opened_hub(wire)
    hub.input("pad1")
    assert wire.sent == []  # nobody has claimed it

    hub.set_target("deck")
    wire.sent.clear()
    hub.input("pad1")
    assert wire.sent == [("deck", "gamepad", True)]


def test_a_controller_going_away_detaches_it_and_releasing_clears_the_target():
    wire = Wire()
    hub, _ = opened_hub(wire)
    hub.set_target("deck")
    wire.sent.clear()

    hub.closed("pad1")
    hub.release("sage")  # not the target: nothing changes
    assert hub.status()["target"] == "deck"
    hub.release("deck")

    assert wire.sent == [("deck", "gamepad_detach", False)]
    assert hub.status() == {"type": "gamepads", "target": None, "controllers": []}


def test_a_tray_app_claims_and_releases_over_its_peer_socket(session):
    gamepad.hub = Hub(peer_control.send, peer_control.broadcast_desktop)
    gamepad.hub.opened(_Open(Controller("pad1", "8BitDo", "event25"), Normaliser(HID_GAMEPAD, BT_RANGES), object()))

    with TestClient(app).websocket_connect("/ws/peer/deck") as socket:
        assert socket.receive_json() == {"type": "gamepads", "target": None,
                                         "controllers": [{"id": "pad1", "name": "8BitDo"}]}
        assert socket.receive_json()["type"] == "kvm"  # the tray's Keyboard & mouse menu
        socket.send_json({"type": "gamepad_claim"})
        state = socket.receive_json()
        assert (state["type"], state["id"], state["buttons"]) == ("gamepad", "pad1", [])
        assert socket.receive_json()["target"] == "deck"

        socket.send_json({"type": "gamepad_release"})
        assert socket.receive_json() == {"type": "gamepad_detach", "id": "pad1"}
        assert socket.receive_json()["target"] is None


# --- HID passthrough ---------------------------------------------------------

def fake_sysfs(tmp_path):
    """The 8BitDo over Bluetooth as sysfs shows it: input device -> HID device."""
    hid = tmp_path / "devices" / "virtual" / "misc" / "uhid" / "0005:2DC8:901B.001E"
    (hid / "hidraw" / "hidraw10").mkdir(parents=True)
    (hid / "report_descriptor").write_bytes(bytes([0x05, 0x01, 0x09, 0x05]))
    (hid / "uevent").write_text("DRIVER=hid-generic\nHID_ID=0005:00002DC8:0000901B\n"
                                "HID_NAME=8BitDo Ultimate 3mode Xbox\nHID_UNIQ=e4:17:d8:3b:34:01\n")
    event_device = hid / "input" / "input80"
    (event_device / "id").mkdir(parents=True)
    (event_device / "id" / "version").write_text("0001\n")
    (event_device / "device").symlink_to(hid, target_is_directory=True)
    link = tmp_path / "class" / "input" / "event25"
    link.mkdir(parents=True)
    (link / "device").symlink_to(event_device, target_is_directory=True)
    return str(tmp_path)


def test_sysfs_gives_what_a_computer_needs_to_recreate_the_controller(tmp_path):
    try:
        root = fake_sysfs(tmp_path)
    except OSError:
        pytest.skip("symlinks not allowed here")

    assert gamepad.hid_info("event25", root) == {
        "name": "8BitDo Ultimate 3mode Xbox", "uniq": "e4:17:d8:3b:34:01", "bus": 5,
        "vendor": 0x2DC8, "product": 0x901B, "version": 1, "country": 0,
        "descriptor": "BQEJBQ==", "hidraw": "hidraw10"}
    assert gamepad.hid_info("event3", root) is None


HID = {"name": "8BitDo", "uniq": "", "bus": 5, "vendor": 0x2DC8, "product": 0x901B,
       "version": 1, "country": 0, "descriptor": "BQEJBQ=="}


def test_a_computer_that_can_create_hid_devices_gets_the_controller_itself(tmp_path):
    wire = Wire()
    with open(tmp_path / "hidraw", "w+b") as hid_file:
        hub, _ = opened_hub(wire, HID, hid_file)
        hub.hello("deck", ["hid", "xbox"])
        hub.set_target("deck")
        hub.input("pad1")                 # evdev: not for a passthrough target
        hub.hid_input("pad1", b"\x03\x08")
        hub.hello("sage", ["xbox"])
        hub.set_target("sage")
        hub.hid_input("pad1", b"\x03\x08")  # raw reports: not for an Xbox-layout target

    assert wire.sent == [("deck", "hid_create", False), ("deck", "hid_input", True),
                         ("deck", "gamepad_detach", False), ("sage", "gamepad", False)]


def test_saying_hello_again_remakes_the_controller_in_the_better_mode(tmp_path):
    wire = Wire()
    with open(tmp_path / "hidraw", "w+b") as hid_file:
        hub, _ = opened_hub(wire, HID, hid_file)
        hub.set_target("deck")            # no hello yet: the Xbox layout
        hub.hello("deck", ["hid", "xbox"])

    assert wire.sent == [("deck", "gamepad", False), ("deck", "gamepad_detach", False), ("deck", "hid_create", False)]


def test_output_from_the_target_reaches_the_real_controller_and_nobody_elses_does(tmp_path):
    wire = Wire()
    path = tmp_path / "hidraw"
    with open(path, "w+b", buffering=0) as hid_file:
        hub, _ = opened_hub(wire, HID, hid_file)
        hub.hello("deck", ["hid"])
        hub.set_target("deck")
        hub.hid_request("sage", {"type": "hid_output", "id": "pad1", "data": "AQI="})
        hub.hid_request("deck", {"type": "hid_output", "id": "pad1", "data": "AwQ="})
    assert path.read_bytes() == b"\x03\x04"



# --- rumble and the controller test ---------------------------------------------

class EventNode:
    def fileno(self):
        return 99


def rumble_hub(monkeypatch, wire, hid=None, hid_file=None):
    played = []

    def fake_play(fd, effect_id, strong, weak, ms):
        played.append((strong, weak, ms))
        return 0 if strong or weak else effect_id

    monkeypatch.setattr(gamepad, "play_rumble", fake_play)
    hub = Hub(wire.send, wire.broadcast)
    pad = Normaliser(HID_GAMEPAD, BT_RANGES)
    hub.opened(_Open(Controller("pad1", "8BitDo", "event25"), pad, EventNode(), hid, hid_file))
    return hub, pad, played


def test_the_rumble_struct_is_the_kernels_ff_effect():
    effect = gamepad.rumble_effect(-1, 0xC000, 0x4000, 70000)
    assert len(effect) == 48
    assert struct.unpack_from("HhHHHHH", effect, 0) == (gamepad.FF_RUMBLE, -1, 0, 0, 0, 65535, 0)
    assert struct.unpack_from("HH", effect, 16) == (0xC000, 0x4000)


def test_rumble_from_the_target_plays_on_the_real_controller(monkeypatch):
    wire = Wire()
    hub, _, played = rumble_hub(monkeypatch, wire)
    hub.set_target("deck")
    hub.rumble("sage", {"type": "gamepad_rumble", "id": "pad1", "strong": 1, "weak": 1, "ms": 100})
    hub.rumble("deck", {"type": "gamepad_rumble", "id": "pad1", "strong": 40000, "weak": 20000, "ms": 250})
    assert played == [(40000, 20000, 250)]

    # Handing the controller to another computer stops a rumble left playing.
    hub.set_target("sage")
    assert played[-1] == (0, 0, 0)


def test_a_passed_through_controller_rumbles_by_output_report_instead(monkeypatch, tmp_path):
    wire = Wire()
    with open(tmp_path / "hidraw", "w+b", buffering=0) as hid_file:
        hub, _, played = rumble_hub(monkeypatch, wire, HID, hid_file)
        hub.hello("deck", ["hid"])
        hub.set_target("deck")
        hub.rumble("deck", {"type": "gamepad_rumble", "id": "pad1", "strong": 1, "weak": 1, "ms": 1})
    assert played == []


def test_the_dashboard_can_vibrate_a_controller_and_hears_when_it_cant(monkeypatch):
    wire = Wire()
    hub, _, played = rumble_hub(monkeypatch, wire)
    assert hub.test_rumble("pad1") is None and played == [(0xC000, 0xC000, 600)]
    assert "isn't connected" in hub.test_rumble("ghost")

    def no_ff(*_):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(gamepad, "play_rumble", no_ff)
    assert "can't vibrate" in hub.test_rumble("pad1")


def test_the_controller_test_gets_live_state_only_while_it_watches(monkeypatch):
    wire = Wire()
    hub, pad, _ = rumble_hub(monkeypatch, wire)
    seen = []
    hub.watch_broadcast = seen.append
    pad.feed(EV_KEY, 304, 1)
    hub.input("pad1")
    assert seen == []  # nobody is looking

    states = hub.watch()
    assert states[0]["id"] == "pad1" and states[0]["buttons"] == ["a"]
    hub.input("pad1")
    assert seen[0]["type"] == "gamepad_state" and seen[0]["buttons"] == ["a"]


def test_the_dashboard_routes_for_the_controller_test(monkeypatch, session):
    wire = Wire()
    hub, pad, played = rumble_hub(monkeypatch, wire)
    monkeypatch.setattr(gamepad, "hub", hub)
    client = TestClient(app)
    assert client.post("/gamepads/watch").json()["controllers"][0]["name"] == "8BitDo"
    assert client.post("/gamepads/pad1/rumble?strength=light").json() == {"ok": True}
    assert played == [(0x4000, 0x8000, 600)]
    response = client.post("/gamepads/ghost/rumble")
    assert response.status_code == 400 and "isn't connected" in response.json()["detail"]
