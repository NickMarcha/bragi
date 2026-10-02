"""The volume/mute cache behind pipewire.get_volume_mute.

Every wpctl get-volume is its own process (~50ms on sagepi), and even run
concurrently a dashboard's worth of them kept a page load near 0.5s. A
reading only goes stale when something changes the node, and every such
change is either Bragi's own write or shows up as a pw-mon event - so those
are the two things that drop it, and nothing else does.
"""

from __future__ import annotations

import asyncio
import threading

from app import pipewire, views, watcher, ws

from .fake_pipewire import HEADSET_CARD_ID

HEADSET_PLAYBACK_NODE = 233


def test_a_second_page_load_reads_no_volumes(session):
    views.build_state()
    session.reset_calls()

    views.build_state()

    assert session.count("wpctl", "get-volume") == 0
    assert session.count("pw-dump") == 1


def test_bragis_own_write_is_never_answered_from_the_cache(session):
    """The throttle worker reads straight back after applying, to broadcast
    what it set. A cached pre-write value there would snap the fader back."""
    assert pipewire.get_volume_mute(HEADSET_PLAYBACK_NODE) == (1.0, False)

    pipewire.set_volume(HEADSET_PLAYBACK_NODE, 0.4)
    assert pipewire.get_volume_mute(HEADSET_PLAYBACK_NODE) == (0.4, False)

    pipewire.set_mute(HEADSET_PLAYBACK_NODE, True)
    assert pipewire.get_volume_mute(HEADSET_PLAYBACK_NODE) == (0.4, True)


def test_a_change_bragi_did_not_make_is_picked_up_from_pw_mon(session, broadcasts):
    """A hardware knob, pavucontrol, or wpctl run by hand on the Pi."""
    pipewire.get_volume_mute(HEADSET_PLAYBACK_NODE)
    session.node(HEADSET_PLAYBACK_NODE).volume = 0.7  # changed behind Bragi's back

    asyncio.run(_feed_pw_mon(["changed:", f"\tid: {HEADSET_PLAYBACK_NODE}"]))

    assert pipewire.get_volume_mute(HEADSET_PLAYBACK_NODE) == (0.7, False)


def test_a_reused_node_id_does_not_inherit_the_old_nodes_reading(session):
    """PipeWire hands a destroyed node's id to the next one. pw-mon reports
    that as removed/added, never as changed."""
    pipewire.get_volume_mute(HEADSET_PLAYBACK_NODE)
    session.node(HEADSET_PLAYBACK_NODE).volume = 0.2

    asyncio.run(_feed_pw_mon(["removed:", f"\tid: {HEADSET_PLAYBACK_NODE}"]))

    assert pipewire.get_volume_mute(HEADSET_PLAYBACK_NODE) == (0.2, False)


def test_a_pw_mon_restart_forgets_everything(session):
    """Events that land while pw-mon is down are never seen, so whatever
    the cache held from before can't be trusted once it's back."""
    pipewire.get_volume_mute(HEADSET_PLAYBACK_NODE)
    session.node(HEADSET_PLAYBACK_NODE).volume = 0.3

    pipewire.forget_volumes()

    assert pipewire.get_volume_mute(HEADSET_PLAYBACK_NODE) == (0.3, False)


def test_a_read_overtaken_by_a_change_is_not_kept(session, monkeypatch):
    """A wpctl read that started before a change and finishes after it holds
    the old value. Storing it would hide the change until the next one."""
    fake_run = session.run
    reading = threading.Event()
    changed = threading.Event()

    def racing_run(args, input_text=None):
        out = fake_run(args, input_text)  # reads the old volume...
        if args[:2] == ["wpctl", "get-volume"] and not changed.is_set():
            reading.set()
            changed.wait(2)  # ...and only returns after the change landed
        return out

    monkeypatch.setattr(pipewire, "_run", racing_run)

    reader = threading.Thread(target=pipewire.get_volume_mute, args=(HEADSET_PLAYBACK_NODE,))
    reader.start()
    assert reading.wait(2)
    session.node(HEADSET_PLAYBACK_NODE).volume = 0.6
    pipewire.forget_volume(HEADSET_PLAYBACK_NODE)
    changed.set()
    reader.join(2)

    assert pipewire.get_volume_mute(HEADSET_PLAYBACK_NODE) == (0.6, False)


async def test_a_dashboard_tab_still_sees_a_knob_turn(session, broadcasts, monkeypatch):
    """End to end through the watcher path: the cached reading must not be
    what gets broadcast after pw-mon reports the change."""
    monkeypatch.setattr(ws, "_WATCHER_COALESCE_SECONDS", 0.01)
    views.build_state()
    session.node(HEADSET_PLAYBACK_NODE).volume = 0.55

    await _feed_pw_mon(["changed:", f"\tid: {HEADSET_PLAYBACK_NODE}"], ws.on_node_changed)
    await ws._drain_task

    sent = broadcasts.of_type("control")[-1]
    assert (sent["key"], sent["direction"], sent["volume"]) == (HEADSET_CARD_ID, "playback", 0.55)


async def _feed_pw_mon(lines: list[str], on_change=None) -> None:
    """Runs watcher._read_events over scripted pw-mon output."""

    class FakeProc:
        stdout = _Lines(lines)

    async def ignore(_node_id: int) -> None:
        pass

    await watcher._read_events(FakeProc(), on_change or ignore)
    await asyncio.sleep(0)


class _Lines:
    def __init__(self, lines: list[str]):
        self._lines = [(line + "\n").encode() for line in lines]

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)
