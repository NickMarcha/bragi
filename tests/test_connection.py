"""What a browser tab receives when it connects.

Found live: with the meters starting the instant a tab connects, level
frames were landing in the new connection's queue while build_state() was
still running (~600ms of pw-dump and wpctl on a Pi 4), so the tab's first
message was a level frame and the state snapshot arrived somewhere behind
it. Harmless for a level frame; not harmless for a control broadcast, which
the late-arriving snapshot would then overwrite with a staler value.
"""

from __future__ import annotations

import asyncio

import pytest

from app import views, ws


class FakeWebSocket:
    """Records what the endpoint sends, and never delivers a message, so the
    connection just sits there the way an idle tab does."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)

    async def receive_json(self) -> dict:
        await asyncio.Event().wait()


@pytest.fixture
def real_manager():
    """websocket_endpoint uses the module-level manager, so this exercises
    the real ConnectionManager rather than the recording stand-in."""
    ws.manager = ws.ConnectionManager()
    yield ws.manager
    ws.manager = ws.ConnectionManager()


async def run_connection(sock, seconds=0.2):
    task = asyncio.create_task(ws.websocket_endpoint(sock))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_the_snapshot_is_the_first_message_a_new_tab_receives(session, real_manager, monkeypatch):
    build_state = views.build_state

    def build_state_while_meters_broadcast():
        ws.manager.broadcast_nowait({"type": "levels", "values": []})
        return build_state()

    monkeypatch.setattr(views, "build_state", build_state_while_meters_broadcast)
    sock = FakeWebSocket()

    await run_connection(sock)

    assert sock.sent[0]["type"] == "state"
    assert [h["key"] for h in sock.sent[0]["headsets"]]


async def test_broadcasts_during_the_snapshot_are_not_lost(session, real_manager, monkeypatch):
    """The tab has to end up with both: the snapshot first, then whatever
    arrived while it was being built."""
    build_state = views.build_state

    def build_state_while_meters_broadcast():
        ws.manager.broadcast_nowait({"type": "levels", "values": []})
        return build_state()

    monkeypatch.setattr(views, "build_state", build_state_while_meters_broadcast)
    sock = FakeWebSocket()

    await run_connection(sock)

    assert [m["type"] for m in sock.sent] == ["state", "levels"]


async def test_connecting_wakes_the_level_meters(session, real_manager):
    """The reconcile loop otherwise waits out its own 2s timer before
    spawning any pw-cat, which then needs another ~0.5-1s to first byte."""
    assert not ws.manager.wake.is_set()

    await run_connection(FakeWebSocket(), seconds=0.05)

    assert ws.manager.wake.is_set()


async def test_a_backlogged_client_drops_level_frames_rather_than_hoarding_them(real_manager):
    """Level frames are only worth anything while they are current, and
    they arrive 20/s for as long as a tab is open. A connection slow enough
    to make send_json apply backpressure used to accumulate them without
    any bound, and would then be shown the whole backlog late - the meters
    fast-forwarding through history instead of showing now."""
    queue = real_manager.register(FakeWebSocket())

    for _ in range(500):
        real_manager.broadcast_levels_nowait({"type": "levels", "values": []})

    assert queue.qsize() <= ws.MAX_QUEUED_LEVEL_FRAMES


async def test_a_backlogged_client_still_gets_every_control_message(real_manager):
    """The bound applies only to level frames. Dropping a control update
    would leave a fader showing the wrong value indefinitely, since nothing
    re-sends it."""
    queue = real_manager.register(FakeWebSocket())

    for _ in range(500):
        real_manager.broadcast_nowait({"type": "control"})

    assert queue.qsize() == 500
