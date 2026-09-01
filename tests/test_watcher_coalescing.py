"""The pw-mon watcher's reaction to graph changes.

pw-mon events arrive in bursts far larger than the number of controls they
represent. Measured on sagepi: one headset profile flip emits 147 "changed"
events across 72 distinct object ids, and the dashboard displays two
controls for that headset. Resolving each id separately, each against its
own pw-dump, was ~7s of subprocess work per click.
"""

from __future__ import annotations

import pytest

from app import ws

from .fake_pipewire import HEADSET_CARD_ID

# The shape of a real profile-flip burst: mostly ports and links the
# dashboard shows nothing for, plus the two headset nodes and one peer node.
HEADSET_PLAYBACK_NODE = 233
HEADSET_CAPTURE_NODE = 124
PEER_SINK_NODE = 50
BURST = set(range(300, 370)) | {HEADSET_PLAYBACK_NODE, HEADSET_CAPTURE_NODE, PEER_SINK_NODE}


@pytest.fixture(autouse=True)
def fast_coalesce(monkeypatch):
    """The window itself is not what is under test - only that the burst is
    resolved as one batch."""
    monkeypatch.setattr(ws, "_WATCHER_COALESCE_SECONDS", 0.01)


async def drain(burst: set[int]) -> None:
    for node_id in sorted(burst):
        await ws.on_node_changed(node_id)
    await ws._drain_task


async def test_burst_costs_one_graph_dump(session, broadcasts):
    await drain(BURST)

    assert session.count("pw-dump") == 1, (
        f"{len(BURST)} changed ids cost {session.count('pw-dump')} pw-dump calls"
    )


async def test_burst_broadcasts_each_affected_control_once(session, broadcasts):
    await drain(BURST)

    controls = [(m["target"], m["key"], m["direction"]) for m in broadcasts.of_type("control")]
    assert sorted(controls) == [
        ("headset", HEADSET_CARD_ID, "capture"),
        ("headset", HEADSET_CARD_ID, "playback"),
        ("peer", "sagedeck", "outgoing"),
    ]


async def test_ids_the_dashboard_shows_nothing_for_broadcast_nothing(session, broadcasts):
    await drain(set(range(300, 370)))

    assert broadcasts.of_type("control") == []


async def test_a_control_a_client_just_moved_is_not_echoed_back(session, broadcasts):
    """Bragi's own wpctl calls raise pw-mon events too. The client that
    caused them already got a fresh broadcast from the throttle worker, so
    echoing a second, possibly staler read at it would drag the fader."""
    control = ("headset", HEADSET_CARD_ID, "playback")
    ws._mark_applied(control)

    await drain({HEADSET_PLAYBACK_NODE})

    assert broadcasts.of_type("control") == []


async def test_a_hardware_knob_turn_still_reaches_the_client(session, broadcasts):
    """The counterpart to the suppression above: nothing recent from a
    client, so this is genuinely new information and must get through."""
    await drain({HEADSET_PLAYBACK_NODE})

    assert [(m["target"], m["key"], m["direction"]) for m in broadcasts.of_type("control")] == [
        ("headset", HEADSET_CARD_ID, "playback")
    ]
