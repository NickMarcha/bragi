"""The HyperX controls on the dashboard: what the headset card carries,
and the WebSocket actions that change them.

The fake dongle answers through the event loop, as the real one does
(its replies reach handle_report from the hidraw node's reader, never from
inside the write), so these wait for the broadcast a reply causes.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from app import headset_hid, views, ws
from app.main import app

from .fake_hyperx import SERIAL, FakeDongle
from .fake_pipewire import HEADSET_CARD_ID, SECOND_CARD_ID


@pytest.fixture
def dongle():
    return FakeDongle()


@pytest.fixture
async def attached(dongle):
    loop = asyncio.get_running_loop()
    hs = headset_hid.attach(SERIAL, dongle.write, lambda serial: loop.create_task(ws.broadcast_headset_controls(serial)))
    dongle.deliver = lambda report: loop.call_soon_threadsafe(hs.handle_report, report)
    hs.refresh()
    await asyncio.sleep(0)
    return hs


@pytest.fixture
def sync_attached(dongle):
    hs = headset_hid.attach(SERIAL, dongle.write, lambda serial: None)
    dongle.deliver = hs.handle_report
    hs.refresh()
    return hs


async def controls_broadcast(broadcasts, check, timeout: float = 2.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        for message in reversed(broadcasts.of_type("headset_controls")):
            if check(message):
                return message
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no matching broadcast in {broadcasts.of_type('headset_controls')}")
        await asyncio.sleep(0.01)


def test_the_cloud_iii_s_card_carries_its_battery(session, sync_attached):
    state = views.build_state()

    by_key = {h["key"]: h for h in state["headsets"]}
    assert by_key[HEADSET_CARD_ID]["controls"]["battery"] == 15
    assert by_key[SECOND_CARD_ID]["controls"] is None


def test_without_a_dongle_the_cards_have_no_controls(session):
    state = views.build_state()

    assert [h["controls"] for h in state["headsets"]] == [None, None]
    html = TestClient(app).get("/").text
    # Rendered but hidden, so a dongle plugged in later can show it live.
    buttons = re.findall(r"<button[^>]*hid-settings-btn[^>]*>", html)
    assert buttons and all(" hidden" in b for b in buttons)


def test_the_dashboard_shows_the_battery(session, sync_attached):
    html = TestClient(app).get("/").text

    assert ">15%<" in html
    assert 'title="Battery 15%"' in html


async def test_mic_monitor_is_set_and_every_tab_hears_the_read_back(session, broadcasts, attached, dongle):
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_mic_monitor", "value": True})

    message = await controls_broadcast(broadcasts, lambda m: m["controls"]["mic_monitor"] is True)
    assert message["key"] == HEADSET_CARD_ID
    assert dongle.mic_monitor is True


async def test_auto_off_is_set_and_read_back(session, broadcasts, attached, dongle):
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_auto_off", "value": 30})

    await controls_broadcast(broadcasts, lambda m: m["controls"]["auto_off_minutes"] == 30)
    assert dongle.auto_off == 1800


async def test_eq_is_set_and_broadcast(session, broadcasts, attached, dongle):
    gains = [6, 4, 2, 0, 0, 0, 0, 2, 4, 6]

    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_eq", "value": gains})

    message = await controls_broadcast(broadcasts, lambda m: m["controls"]["eq"] == gains)
    assert message["key"] == HEADSET_CARD_ID
    assert len(dongle.sent(0x5F)) == 10


async def test_a_bad_value_changes_nothing(session, broadcasts, attached, dongle):
    for action, value in (("set_auto_off", 7), ("set_eq", [99] * 10), ("set_mic_monitor", "yes")):
        await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": action, "value": value})

    assert [w for w in dongle.writes if w[3] == 0x00] == []


async def test_a_headset_without_a_dongle_ignores_the_actions(session, broadcasts):
    await ws.apply_action({"target": "headset", "key": SECOND_CARD_ID, "action": "set_mic_monitor", "value": True})

    assert broadcasts.of_type("headset_controls") == []


async def test_a_battery_notification_reaches_every_tab(session, broadcasts, attached, dongle):
    dongle.notify(0x01, 9)

    message = await controls_broadcast(broadcasts, lambda m: m["controls"]["battery"] == 9)
    assert message["key"] == HEADSET_CARD_ID


BASS_BOOST = [6, 5, 4, 2, 0, 0, 0, 0, 0, 0]


async def test_applying_a_preset_sends_its_eq_and_names_it(session, broadcasts, attached, dongle):
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "apply_eq_preset",
                           "value": "Bass boost"})

    message = await controls_broadcast(broadcasts, lambda m: m["controls"]["eq"] == BASS_BOOST)
    assert message["controls"]["eq_preset"] == "Bass boost"
    assert dongle.sent(0x5F)[0][:9] == bytes.fromhex("0c 02 03 00 00 5f 00 02 58")  # +6.00 dB
    assert len(dongle.sent(0x5F)) == 10


async def test_moving_a_slider_off_a_preset_makes_it_custom(session, broadcasts, attached, dongle):
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_eq",
                           "value": [5.5] + BASS_BOOST[1:]})

    message = await controls_broadcast(broadcasts, lambda m: m["controls"]["eq"] is not None)
    assert message["controls"]["eq_preset"] is None


async def test_an_unknown_preset_sends_nothing(session, broadcasts, attached, dongle):
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "apply_eq_preset",
                           "value": "Nope"})

    assert dongle.sent(0x5F) == []


async def test_saving_a_preset_reaches_every_tab_and_names_the_current_eq(session, broadcasts, attached, dongle):
    gains = [2, 2, 1, 0, 0, 0, 0, 1, 2, 2]
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_eq", "value": gains})
    await controls_broadcast(broadcasts, lambda m: m["controls"]["eq"] == gains)

    await ws.apply_action({"action": "save_eq_preset", "value": {"name": "Mine", "gains": gains}})

    presets = broadcasts.of_type("eq_presets")[-1]["presets"]
    assert presets[-1]["name"] == "Mine"
    await controls_broadcast(broadcasts, lambda m: m["controls"]["eq_preset"] == "Mine")


async def test_deleting_a_preset_reaches_every_tab(session, broadcasts):
    await ws.apply_action({"action": "save_eq_preset", "value": {"name": "Mine", "gains": [1] * 10}})
    await ws.apply_action({"action": "delete_eq_preset", "value": "Mine"})

    assert "Mine" not in [p["name"] for p in broadcasts.of_type("eq_presets")[-1]["presets"]]


async def test_a_bad_preset_save_or_delete_changes_nothing(session, broadcasts):
    for action in ({"action": "save_eq_preset", "value": {"name": "Flat", "gains": [1] * 10}},
                   {"action": "save_eq_preset", "value": {"name": "Loud", "gains": [99] * 10}},
                   {"action": "save_eq_preset", "value": "Mine"},
                   {"action": "delete_eq_preset", "value": "Flat"}):
        await ws.apply_action(action)

    assert broadcasts.of_type("eq_presets") == []


def test_the_dialog_offers_the_presets(session, sync_attached):
    state = views.build_state()
    assert state["eq_presets"][0]["name"] == "Flat"

    html = TestClient(app).get("/").text
    assert ">Bass boost</option>" in html
    assert "More low end for music and films." in html
