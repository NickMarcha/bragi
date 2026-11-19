"""The main headset (app/headset_choice.py, bragi#59)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import headset_choice, headset_hid, pipewire
from app.main import app

from .fake_pipewire import HEADSET_CARD_ID, FakeNode

AT2020 = "usb-audio-technica_AT2020USB_-00"
HEADSET_OUT = f"alsa_output.{HEADSET_CARD_ID}.analog-stereo"
AT2020_OUT = f"alsa_output.{AT2020}.analog-stereo"


@pytest.fixture
def desk(session, monkeypatch):
    """The demo headset plus an AT2020 listed first, and a pw-metadata/wpctl
    stand-in for the default output."""
    session.free_nodes[:0] = [
        FakeNode(301, AT2020_OUT, "AT2020USB+ Analog Stereo", "Audio/Sink"),
        FakeNode(302, f"alsa_input.{AT2020}.analog-stereo", "AT2020USB+ Analog Stereo", "Audio/Source"),
    ]
    monkeypatch.setattr(headset_hid, "find_devices", lambda: {"hidraw0": "0000000001"})
    state = {"default": AT2020_OUT, "set": []}
    real = pipewire._run

    def run(args, input_text=None):
        if args[0] == "pw-metadata":
            return f"update: id:0 key:'default.audio.sink' value:'{json.dumps({'name': state['default']})}' type:'Spa:String:JSON'\n"
        if args[:2] == ["wpctl", "set-default"]:
            state["set"].append(int(args[2]))
            state["default"] = next(n.name for n in session.live_nodes() if n.id == int(args[2]))
            return ""
        return real(args, input_text)

    monkeypatch.setattr(pipewire, "_run", run)
    return state


def test_automatic_picks_the_hyperx_not_the_desk_mic(desk):
    assert headset_choice.card() == HEADSET_CARD_ID
    assert headset_choice.output_name() == HEADSET_OUT


def test_without_a_dongle_it_picks_the_first_card_with_both_ends(desk, monkeypatch):
    monkeypatch.setattr(headset_hid, "find_devices", lambda: {})
    assert headset_choice.card() == HEADSET_CARD_ID  # sorted: "usb-HP__…" before "usb-audio…"


def test_a_choice_wins_and_falls_back_while_unplugged(desk, session):
    headset_choice.save(AT2020)
    assert headset_choice.output_name() == AT2020_OUT
    session.free_nodes[:] = [n for n in session.free_nodes if AT2020 not in n.name]
    assert headset_choice.output_name() == HEADSET_OUT
    assert headset_choice.view()["missing"] is True


def test_the_default_output_is_put_back_on_the_headset(desk):
    assert headset_choice.keep_default_output() is True
    assert desk["default"] == HEADSET_OUT and len(desk["set"]) == 1
    assert headset_choice.keep_default_output() is False  # already right: no wpctl call
    assert len(desk["set"]) == 1


def test_choosing_in_settings(desk):
    client = TestClient(app)
    page = client.get("/headset-choice").text
    assert "Main headset" in page and "AT2020USB+" in page and "Automatic" in page
    page = client.post("/headset-choice", data={"card": AT2020}).text
    assert headset_choice.saved() == AT2020 and desk["default"] == AT2020_OUT
    assert "isn&#39;t connected" in client.post("/headset-choice", data={"card": "usb-nothing-00"}).text
