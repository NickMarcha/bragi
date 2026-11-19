"""A second headset that hears what the main one hears (app/second_headset.py, bragi#75)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import headset_hid, second_headset
from app.main import app

from .fake_pipewire import HEADSET_CARD_ID, FakeNode

OTHER = "usb-HP__Inc_HyperX_Cloud_III_Wireless_0000000000000000-00"
AT2020 = "usb-audio-technica_AT2020USB_-00"
MAIN_OUT = f"alsa_output.{HEADSET_CARD_ID}.analog-stereo"
OTHER_OUT = f"alsa_output.{OTHER}.analog-stereo"


class FakeProcess:
    def __init__(self, args, **_):
        self.args, self.done = args, None

    def poll(self):
        return self.done

    def terminate(self):
        self.done = 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def desk(session, monkeypatch):
    """The main headset, a second HyperX and an AT2020 (a mic with a headphone jack)."""
    session.free_nodes[:0] = [
        FakeNode(301, f"alsa_output.{AT2020}.analog-stereo", "AT2020USB+ Analog Stereo", "Audio/Sink"),
        FakeNode(302, f"alsa_input.{AT2020}.analog-stereo", "AT2020USB+ Analog Stereo", "Audio/Source"),
        FakeNode(311, OTHER_OUT, "HyperX Cloud III Wireless Analog Stereo", "Audio/Sink"),
        FakeNode(312, f"alsa_input.{OTHER}.mono-fallback", "HyperX Cloud III Wireless Mono", "Audio/Source"),
    ]
    monkeypatch.setattr(headset_hid, "find_devices", lambda: {"hidraw0": "0000000001"})
    started = []

    def popen(args, **kw):
        started.append(FakeProcess(args))
        return started[-1]

    monkeypatch.setattr(second_headset.subprocess, "Popen", popen)
    monkeypatch.setattr(second_headset, "_process", None)
    monkeypatch.setattr(second_headset, "_running_for", None)
    return {"session": session, "started": started}


def test_off_by_default(desk):
    assert second_headset.saved() == "off"
    assert second_headset.reconcile() == "off" and not desk["started"]


def test_automatic_finds_the_other_hyperx_not_the_desk_mic(desk):
    second_headset.save("auto")
    assert second_headset.wanted() == (MAIN_OUT, OTHER_OUT)
    assert second_headset.reconcile() == "playing"
    args = desk["started"][0].args
    assert args[0] == "pw-loopback"
    assert f"target.object={MAIN_OUT}" in args[args.index("--capture-props") + 1]
    assert "stream.capture.sink=true" in args[args.index("--capture-props") + 1]
    assert f"target.object={OTHER_OUT}" in args[args.index("--playback-props") + 1]
    assert second_headset.reconcile() == "playing" and len(desk["started"]) == 1  # left alone


def test_switching_the_second_headset_off_stops_the_copy(desk):
    second_headset.save("auto")
    second_headset.reconcile()
    desk["session"].free_nodes[:] = [n for n in desk["session"].free_nodes if OTHER not in n.name]
    assert second_headset.reconcile() == "off"
    assert desk["started"][0].done == 0


def test_a_picked_device_and_bad_picks(desk):
    second_headset.choose(f"{AT2020}")
    assert second_headset.wanted() == (MAIN_OUT, f"alsa_output.{AT2020}.analog-stereo")
    with pytest.raises(ValueError, match="main headset"):
        second_headset.choose(HEADSET_CARD_ID)
    with pytest.raises(ValueError, match="isn't connected"):
        second_headset.choose("usb-nothing-00")


def test_the_settings_section(desk):
    client = TestClient(app)
    page = client.get("/headset-choice").text
    assert "Second headset" in page and "Another HyperX headset" in page
    page = client.post("/second-headset", data={"card": "auto"}).text
    assert second_headset.saved() == "auto" and "Playing on" in page
    page = client.post("/second-headset", data={"card": "off"}).text
    assert second_headset.saved() == "off" and desk["started"][0].done == 0
