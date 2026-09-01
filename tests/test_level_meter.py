"""Live level metering: what actually goes out over the WebSocket.

The meters were measured working (all 8 at ~20Hz on sagepi) but expensive
and edge-buggy. One dashboard client cost 160 WebSocket frames per second -
8 metered directions x 20Hz, one frame each - which showed up as 39% CPU in
tailscaled alone. The information in those 160 frames fits in 20.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct

import pytest

from app import level_meter

from .fake_pipewire import HEADSET_CARD_ID as HYPERX_CARD

HEADSET_PLAYBACK = ("headset", "hyperx", "playback")
HEADSET_CAPTURE = ("headset", "hyperx", "capture")
PEER_INCOMING = ("peer", "sagedeck", "incoming")


class FakeCapture:
    """Stands in for pw-cat's stdout: hands out a fixed number of
    fixed-amplitude s16 mono chunks, then goes quiet like a live capture
    with nothing playing through it."""

    def __init__(self, amplitude: int, chunks: int):
        self.amplitude = amplitude
        self.remaining = chunks
        self.exhausted = asyncio.Event()

    async def readexactly(self, n: int) -> bytes:
        if self.remaining <= 0:
            self.exhausted.set()
            await asyncio.Event().wait()  # a live capture just stops producing
        self.remaining -= 1
        await asyncio.sleep(0)  # a real stream yields to the loop
        samples = n // 2
        return struct.pack(f"<{samples}h", *([self.amplitude] * samples))


async def read_chunks(stream, control):
    """Runs the reader until the capture is exhausted, then stops it."""
    task = asyncio.create_task(level_meter._read_levels(stream, *control))
    await asyncio.wait_for(stream.exhausted.wait(), timeout=5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_a_minus_20_dbfs_signal_reads_at_60_percent(broadcasts):
    """The meter is dB-mapped with a -50dBFS floor and 0dBFS top (see
    _MIN_DB), so -20dBFS lands at (-20 - -50) / 50 = 0.6. A linear meter
    would read this as 0.1 and look like it was barely moving."""
    minus_20_dbfs = int(0.1 * 32768)

    await read_chunks(FakeCapture(minus_20_dbfs, chunks=200), HEADSET_PLAYBACK)

    assert level_meter.current_levels()[HEADSET_PLAYBACK] == pytest.approx(0.6, abs=0.02)


async def test_silence_reads_at_zero(broadcasts):
    await read_chunks(FakeCapture(0, chunks=50), HEADSET_PLAYBACK)

    assert level_meter.current_levels()[HEADSET_PLAYBACK] == 0.0


async def test_one_frame_carries_every_meter(broadcasts):
    """The whole point: one WebSocket frame per tick, not one per meter."""
    for control in (HEADSET_PLAYBACK, HEADSET_CAPTURE, PEER_INCOMING):
        level_meter.report_level(control, 0.5)

    await level_meter.broadcast_levels_once()

    assert len(broadcasts.messages) == 1
    frame = broadcasts.messages[0]
    assert frame["type"] == "levels"
    assert frame["values"] == [
        {"target": "headset", "key": "hyperx", "direction": "capture", "value": 0.5},
        {"target": "headset", "key": "hyperx", "direction": "playback", "value": 0.5},
        {"target": "peer", "key": "sagedeck", "direction": "incoming", "value": 0.5},
    ]


async def test_nothing_is_sent_when_no_meters_are_running(broadcasts):
    await level_meter.broadcast_levels_once()

    assert broadcasts.messages == []


async def test_a_stopped_capture_falls_to_zero(broadcasts):
    """A headset being disabled cancels its capture. Without a final zero
    the bar just freezes at whatever height it was last painted at."""
    level_meter.report_level(HEADSET_PLAYBACK, 0.8)
    level_meter.stop_level(HEADSET_PLAYBACK)

    await level_meter.broadcast_levels_once()

    assert broadcasts.messages[0]["values"] == [
        {"target": "headset", "key": "hyperx", "direction": "playback", "value": 0.0}
    ]


async def test_a_stopped_capture_drops_out_after_its_final_zero(broadcasts):
    level_meter.report_level(HEADSET_PLAYBACK, 0.8)
    level_meter.stop_level(HEADSET_PLAYBACK)

    await level_meter.broadcast_levels_once()
    await level_meter.broadcast_levels_once()

    assert len(broadcasts.messages) == 1


class NeverEndingCapture:
    """A capture that is running but has produced nothing yet."""

    def __init__(self, started: list):
        self.started = started

    async def __call__(self, target, key, direction, pw_target):
        self.started.append((target, key, direction))
        await asyncio.Event().wait()


async def test_metering_starts_the_moment_a_client_connects(session, broadcasts, monkeypatch):
    """supervise() reconciles on a 2s timer. Waiting out that timer before
    even spawning pw-cat (which itself takes ~0.5-1s to first byte) left the
    meters sitting dead for ~3s every time the dashboard was opened."""
    started: list = []
    monkeypatch.setattr(level_meter, "_monitor_node", NeverEndingCapture(started))
    broadcasts._clients = False

    supervisor = asyncio.create_task(level_meter.supervise())
    try:
        await asyncio.sleep(0.05)
        assert started == [], "nothing should be metered for a dashboard nobody has open"

        broadcasts.set_clients(True)
        await asyncio.sleep(0.05)

        assert sorted(started) == [
            ("headset", HYPERX_CARD, "capture"),
            ("headset", HYPERX_CARD, "playback"),
            ("peer", "sage", "incoming"),
            ("peer", "sage", "outgoing"),
            ("peer", "sage-dev", "incoming"),
            ("peer", "sage-dev", "outgoing"),
            ("peer", "sagedeck", "incoming"),
            ("peer", "sagedeck", "outgoing"),
        ]
    finally:
        supervisor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await supervisor
