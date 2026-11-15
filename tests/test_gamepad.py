"""Game controllers on the Bragi host go to the computer that claimed them
(app/gamepad.py), as one Xbox layout whatever the kernel driver reports."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import gamepad, peer_control
from app.gamepad import (ABS_BRAKE, ABS_GAS, ABS_HAT0X, ABS_RX, ABS_RZ, ABS_X, ABS_Y, ABS_Z, EV_ABS,
                         EV_KEY, EV_SYN, HID_GAMEPAD, XPAD, Controller, Hub, Normaliser)
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


def opened_hub(wire):
    hub = Hub(wire.send, wire.broadcast)
    pad = Normaliser(HID_GAMEPAD, BT_RANGES)
    hub.opened(Controller("pad1", "8BitDo", "event25"), pad, object())
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
    gamepad.hub.opened(Controller("pad1", "8BitDo", "event25"), Normaliser(HID_GAMEPAD, BT_RANGES), object())

    with TestClient(app).websocket_connect("/ws/peer/deck") as socket:
        assert socket.receive_json() == {"type": "gamepads", "target": None,
                                         "controllers": [{"id": "pad1", "name": "8BitDo"}]}
        socket.send_json({"type": "gamepad_claim"})
        state = socket.receive_json()
        assert (state["type"], state["id"], state["buttons"]) == ("gamepad", "pad1", [])
        assert socket.receive_json()["target"] == "deck"

        socket.send_json({"type": "gamepad_release"})
        assert socket.receive_json() == {"type": "gamepad_detach", "id": "pad1"}
        assert socket.receive_json()["target"] is None
