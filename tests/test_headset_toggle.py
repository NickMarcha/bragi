"""Enabling and disabling a headset - the slowest thing in the dashboard.

Measured on the live sagepi before these tests existed: 9.7s from click to
the UI updating. The cost is subprocess calls, so that is what is asserted
here (see fake_pipewire's docstring for the per-call costs).
"""

from __future__ import annotations

import pytest

from app import ws

from .fake_pipewire import HEADSET_CARD_ID, OFF_PROFILE, SECOND_CARD_ID

TOGGLE = {"target": "headset", "action": "toggle_enabled", "key": HEADSET_CARD_ID}


async def test_toggle_disables_the_card_and_announces_it(session, broadcasts):
    await ws.apply_action(TOGGLE)

    assert session.card(90).enabled is False
    announced = broadcasts.of_type("headset")[-1]
    assert announced["key"] == HEADSET_CARD_ID
    assert announced["enabled"] is False


async def test_toggle_re_enables_a_disabled_card(session, broadcasts):
    """A disabled card has no Sink/Source nodes at all, so this only works
    if the card is resolved as a Device rather than through its nodes."""
    await ws.apply_action({**TOGGLE, "key": SECOND_CARD_ID})

    assert session.card(91).enabled is True
    announced = broadcasts.of_type("headset")[-1]
    assert announced["key"] == SECOND_CARD_ID
    assert announced["enabled"] is True


async def test_toggle_dumps_the_graph_once(session, broadcasts):
    """One pw-dump carries both the Nodes and the Devices this needs, and
    ~100ms each is the whole reason the toggle felt slow."""
    await ws.apply_action(TOGGLE)

    assert session.count("pw-dump") == 1, f"pw-dump calls: {session.count('pw-dump')}"


async def test_enabling_picks_the_highest_priority_real_profile(session, broadcasts):
    """The fake card offers analog-stereo+mono at 6501 and analog-stereo at
    6500; "off" is never the answer to "turn this on"."""
    await ws.apply_action({**TOGGLE, "key": SECOND_CARD_ID})

    assert session.card(91).active_index == 1


async def test_a_card_with_nothing_to_restore_is_left_alone(session, broadcasts):
    """A card can momentarily enumerate no usable profile - PipeWire
    re-enumerates a USB card's profiles as it comes and goes. Falling back
    to "off" there meant a click asking to *enable* the headset silently
    disabled it again and reported it as off, which is what a stuck
    enable button looks like from the outside."""
    card = session.card(91)
    card.profiles = [(OFF_PROFILE, "off", 0)]

    await ws.apply_action({**TOGGLE, "key": SECOND_CARD_ID})

    assert session.count("wpctl", "set-profile") == 0
    assert card.active_index == OFF_PROFILE
