"""HID++ mouse buttons and battery (app/hidpp.py), against a simulated
receiver with an MX Master 3S in slot 2."""

from __future__ import annotations

import asyncio
import json

from app import actions, hidpp, kvm_config
from app.hidpp import LONG, SHORT, SWID, Link

NAME = b"MX Master 3S"
# Feature indexes the simulated mouse has.
FEATURES = {hidpp.DEVICE_NAME: 3, hidpp.REPROG_CONTROLS: 9, hidpp.UNIFIED_BATTERY: 6}
# Its controls: (cid, flags). 0x52 is the middle button (plain, left alone),
# 0xd4 is virtual.
CONTROLS = [(0x52, 0x30), (0xC3, 0x30), (0xC4, 0x30), (0xD4, 0xA0)]


class Receiver:
    """Answers HID++ requests like a Bolt receiver with one mouse paired."""

    def __init__(self):
        self.link: Link | None = None
        self.diversions: list[tuple[int, int]] = []
        self.registers = {0x00: bytearray(3)}

    def write(self, message: bytes) -> None:
        reply = self.answer(bytes(message))
        if reply is not None:
            asyncio.get_running_loop().call_soon(self.link.feed, reply)

    def answer(self, m: bytes) -> bytes | None:
        if m[0] == SHORT and m[1] == 0xFF:
            if m[2] == 0x81:
                return bytes([SHORT, 0xFF, 0x81, 0x00, *self.registers[0x00]])
            if m[2] == 0x80:
                self.registers[0x00] = bytearray(m[4:7])
                return bytes([SHORT, 0xFF, 0x80, 0x00, 0, 0, 0])
        dev, feature, fn = m[1], m[2], m[3] >> 4
        if dev != 2:
            return bytes([SHORT, dev, 0x8F, feature, m[3], 0x09, 0])  # nothing paired there
        params = m[4:]

        def reply(*data):
            return bytes([LONG, dev, feature, m[3], *data]).ljust(20, b"\0")

        if feature == 0:
            return reply(FEATURES.get((params[0] << 8) | params[1], 0))
        if feature == 3:
            return reply(len(NAME)) if fn == 0 else reply(*NAME[params[0]:params[0] + 16])
        if feature == 9 and fn == 0:
            return reply(len(CONTROLS))
        if feature == 9 and fn == 1:
            cid, flags = CONTROLS[params[0]]
            return reply(cid >> 8, cid & 0xFF, 0, 0, flags)
        if feature == 9 and fn == 3:
            self.diversions.append(((params[0] << 8) | params[1], params[2]))
            return reply(*params[:5])
        if feature == 6 and fn == 1:
            return reply(65, 4, 0)  # 65 %, discharging
        return bytes([LONG, dev, 0xFF, feature, m[3], 0x02]).ljust(20, b"\0")


def press_frame(*cids):
    data = [LONG, 2, 9, 0x00]
    for cid in cids:
        data += [cid >> 8, cid & 0xFF]
    return bytes(data).ljust(20, b"\0")


def make_link(wanted=lambda name, cid: False):
    receiver = Receiver()
    pressed, connected = [], []
    link = Link("/dev/hidraw7", receiver.write, lambda _l, dev, cid: pressed.append((dev.name, cid)),
                lambda _l, index: connected.append(index), wanted)
    receiver.link = link
    return receiver, link, pressed, connected


def test_a_paired_mouse_is_learned_and_its_buttons_set_back_to_normal():
    receiver, link, _, _ = make_link()

    async def run():
        await link.enable_notifications()
        for index in range(1, 7):
            await link.probe(index)

    asyncio.run(run())
    dev = link.devices[2]
    assert (dev.name, dev.controls) == ("MX Master 3S", [0xC3, 0xC4])
    assert dev.battery["percent"] == 65 and dev.battery["state"] == "discharging"
    # Set explicitly, so the fork's old diversion can't swallow them.
    assert receiver.diversions == [(0xC3, hidpp.DIVERT_VALID), (0xC4, hidpp.DIVERT_VALID)]
    assert receiver.registers[0x00][1] & 0x01  # wireless notifications on
    assert set(link.devices) == {2} and not link.unanswered  # empty slots said so


def test_a_button_with_an_action_is_diverted_and_its_presses_come_through():
    wants = {("MX Master 3S", 0xC3)}
    receiver, link, pressed, connected = make_link(lambda name, cid: (name, cid) in wants)

    async def run():
        await link.probe(2)
        link.feed(press_frame(0xC3))
        link.feed(press_frame(0xC3))  # still held: no second press
        link.feed(press_frame())
        link.feed(press_frame(0xC3))
        link.feed(bytes([SHORT, 2, hidpp.CONNECTION_NOTICE, 0x10, 0x00, 0, 0]))  # reconnected

    asyncio.run(run())
    assert receiver.diversions[0] == (0xC3, hidpp.DIVERT | hidpp.DIVERT_VALID)
    assert pressed == [("MX Master 3S", 0xC3), ("MX Master 3S", 0xC3)]
    assert connected == [2]


def test_a_press_runs_the_saved_action(monkeypatch, tmp_path):
    monkeypatch.setattr(hidpp, "SETTINGS_FILE", tmp_path / "mouse_buttons.yaml")
    ran = []

    async def fake_run(action):
        ran.append(action)
        return True, "switched to sagedeck"

    monkeypatch.setattr(actions, "run_kvm", fake_run)
    manager = hidpp.Manager()

    async def run():
        assert (await manager.set_action("MX Master 3S", 0xC3, {"type": "kvm_next"})) == {"type": "kvm_next"}
        dev = hidpp.Device(2, name="MX Master 3S", reprog=9, controls=[0xC3])
        manager.on_press(None, dev, 0xC3)
        manager.on_press(None, dev, 0xC4)  # no action
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert ran == [{"type": "kvm_next"}]
    assert hidpp.action_for("MX Master 3S", 0xC3) == {"type": "kvm_next"}
    assert manager._results[("MX Master 3S", 0xC3)]["text"] == "switched to sagedeck"


def test_handing_the_buttons_over_turns_the_kvms_listener_off(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAGI_KVM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "env").write_text('DESKFLOW_EVDEV_HIDPP=1\nXKB_DEFAULT_LAYOUT="no"\n')
    (tmp_path / "bindings.json").write_text(json.dumps({"bindings": [
        {"device": "MX Master 3S", "code": 4291, "send": [29, 56, 67]},
        {"device": "HyperX", "code": 163, "send": [29, 56, 66]}], "remaps": []}))
    monkeypatch.setattr(kvm_config, "_restart", lambda: True)

    before = kvm_config.fork_hidpp()
    assert before["listening"] and [b["code"] for b in before["bindings"]] == [4291]
    assert "Bragi handles the mouse buttons" in kvm_config.hand_over_hidpp()
    assert kvm_config.fork_hidpp() == {"listening": False, "bindings": []}
    assert (tmp_path / "env").read_text() == 'XKB_DEFAULT_LAYOUT="no"\n'
    assert [b["code"] for b in json.loads((tmp_path / "bindings.json").read_text())["bindings"]] == [163]


def test_logitech_hidpp_nodes_are_found_by_vendor_and_long_report(tmp_path):
    for name, uevent, descriptor in (("hidraw7", "HID_ID=0003:0000046D:0000C548", b"\x06\x00\xff\x85\x11"),
                                     ("hidraw8", "HID_ID=0003:0000046D:0000C548", b"\x05\x01"),
                                     ("hidraw0", "HID_ID=0003:000003F0:000006BE", b"\x85\x11")):
        d = tmp_path / "class" / "hidraw" / name / "device"
        d.mkdir(parents=True)
        (d / "uevent").write_text(uevent + "\n")
        (d / "report_descriptor").write_bytes(descriptor)
    assert hidpp.find_nodes(str(tmp_path)) == ["/dev/hidraw7"]


def test_the_kvm_setup_lists_mouse_buttons_and_saves_an_action(monkeypatch, tmp_path, session):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(hidpp, "SETTINGS_FILE", tmp_path / "mouse_buttons.yaml")
    monkeypatch.setenv("BRAGI_KVM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(actions, "computers", lambda: ["sage", "sagedeck"])
    manager = hidpp.Manager()
    link = Link("/dev/hidraw7", lambda m: None, manager.on_press, manager.on_connect, manager.wanted)
    link.devices[2] = hidpp.Device(2, name="MX Master 3S", reprog=9, controls=[0xC3],
                                   battery={"percent": 65, "state": "discharging", "at": 0})
    manager.links[link.path] = link
    monkeypatch.setattr(hidpp, "manager", manager)
    client = TestClient(app)

    view = client.get("/mouse-buttons").json()
    assert view["devices"][0]["controls"][0]["label"] == "Thumb button"
    assert view["computers"] == ["sage", "sagedeck"]
    saved = client.post("/mouse-buttons", json={"device": "MX Master 3S", "control": "0xc3",
                                                "action": {"type": "kvm_switch", "computer": "sage"}}).json()
    assert saved["devices"][0]["controls"][0]["action"] == {"type": "kvm_switch", "computer": "sage"}
    assert client.post("/mouse-buttons", json={"device": "MX Master 3S", "control": "0xc3",
                                               "action": {"type": "rm -rf"}}).status_code == 400
