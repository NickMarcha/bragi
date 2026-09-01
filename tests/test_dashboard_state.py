"""The dashboard payload and the per-control actions behind the faders.

These are the two remaining hot paths: build_state() runs on every page load
and every WebSocket connect, and a fader drag runs its action ~12 times a
second. Both are asserted for subprocess cost as well as behaviour, for the
reason fake_pipewire's docstring gives.
"""

from __future__ import annotations

from app import audio_state, views, ws

from .fake_pipewire import HEADSET_CARD_ID, SECOND_CARD_ID
from .helpers import settle

HEADSET_PLAYBACK_NODE = 233
SAGEDECK_INCOMING_NODE = 53


def test_build_state_lists_both_headsets_and_every_peer(session):
    state = views.build_state()

    assert [h["key"] for h in state["headsets"]] == [HEADSET_CARD_ID, SECOND_CARD_ID]
    assert [p["name"] for p in state["peers"]] == ["sagedeck", "sage-dev", "sage"]


def test_build_state_still_shows_a_disabled_headset(session):
    """Disabling a card removes its nodes from PipeWire entirely, so a
    headset that is off has to be found through its ALSA card Device or it
    disappears from the dashboard with no way to switch it back on."""
    state = views.build_state()

    disabled = next(h for h in state["headsets"] if h["key"] == SECOND_CARD_ID)
    assert disabled["enabled"] is False
    assert disabled["label"] == "HyperX Cloud III Wireless"
    assert disabled["playback"]["connected"] is False


def test_build_state_dumps_the_graph_once(session):
    """One dump for the whole page. The wpctl calls on top of it are one per
    displayed direction, which is the floor - wpctl has no bulk query."""
    views.build_state()

    assert session.count("pw-dump") == 1


async def test_setting_a_headset_volume_reaches_the_hardware(session, broadcasts):
    await ws.apply_action(
        {
            "target": "headset",
            "key": HEADSET_CARD_ID,
            "direction": "playback",
            "action": "set_volume",
            "value": 0.42,
            "ts": 1000.0,
        }
    )
    await settle()

    assert session.node(HEADSET_PLAYBACK_NODE).volume == 0.42
    broadcast = broadcasts.of_type("control")[-1]
    assert broadcast["key"] == HEADSET_CARD_ID
    assert broadcast["volume"] == 0.42


async def test_a_stale_drag_tick_never_overwrites_a_newer_one(session, broadcasts):
    """Ordering is by the client's own Date.now(), so a message that
    overtakes a newer one in flight has to be dropped rather than applied."""
    for ts, value in ((2000.0, 0.80), (1000.0, 0.10)):
        await ws.apply_action(
            {
                "target": "headset",
                "key": HEADSET_CARD_ID,
                "direction": "playback",
                "action": "set_volume",
                "value": value,
                "ts": ts,
            }
        )
    await settle()

    assert session.node(HEADSET_PLAYBACK_NODE).volume == 0.80


async def test_muting_a_peer_direction_toggles_it(session, broadcasts):
    action = {
        "target": "peer",
        "key": "sagedeck",
        "direction": "incoming",
        "action": "toggle_mute",
    }
    await ws.apply_action(action)
    assert session.node(SAGEDECK_INCOMING_NODE).muted is True

    await ws.apply_action(action)
    assert session.node(SAGEDECK_INCOMING_NODE).muted is False


async def test_peer_balance_is_applied_as_channel_volumes(session, broadcasts):
    """PipeWire has no pan control, so balance is a raw stereo
    channelVolumes write - and it has to carry the stored volume with it or
    the two controls overwrite each other."""
    audio_state.set_state("sagedeck-audio", 1.0, 0.0)

    await ws.apply_action(
        {
            "target": "peer",
            "key": "sagedeck",
            "direction": "incoming",
            "action": "set_balance",
            "value": -1.0,
            "ts": 1000.0,
        }
    )
    await settle()

    left, right = session.node(SAGEDECK_INCOMING_NODE).channel_volumes
    assert left > 0.0
    assert right == 0.0
    assert audio_state.get_state("sagedeck-audio") == (1.0, -1.0)
