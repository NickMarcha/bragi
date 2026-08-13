"""The realtime control plane: one WebSocket per browser tab, broadcasting
per-control updates and applying incoming volume/mute/balance actions.

Replaces the old per-control POST routes + 3s card polling. Domain logic
(pipewire/headsets/peers/audio_state) is unchanged from that version -
only the transport moved here.

Two things that weren't obvious until tested live against sagepi's real
PipeWire graph, both still true and worth knowing before touching this file:

1. Every broadcast source (a client's own throttled slider action, the
   pw-mon watcher reacting to a hardware-knob turn, another client's
   action) only ever enqueues onto each connection's own asyncio.Queue -
   never calls websocket.send_json() directly. Only the connection's own
   task, inside websocket_endpoint(), ever touches that socket, for both
   send and receive. With multiple independent broadcasters all calling
   send() directly, a background task's send() could land while the
   connection's own task was mid-receive() and corrupt Starlette's
   application_state, crashing the connection with a "WebSocket is not
   connected" RuntimeError on the next receive.
2. A broadcast must never rebuild the *whole* dashboard state
   (views.build_state()) - that's ~11 sequential wpctl/pw-dump subprocess
   spawns, ~625ms on a Pi 4. Doing that after every single throttled slider
   tick made "live while dragging" effectively ~1s-laggy, defeating the
   entire point of this rewrite. Every action broadcasts only the one
   control that changed (views.headset_control_view/peer_control_view,
   1-2 subprocess calls, ~100-150ms) - build_state() is reserved for the
   one-time initial snapshot on connect.
3. websocket_endpoint's receive loop must never `await apply_action(...)`
   inline - that blocks reading the *next* message until the current one's
   real subprocess work finishes, which starves _throttled_apply's own
   per-key coalescing of any chance to run (each message ends up looking
   "due" by the time it's finally read, since the previous message's
   processing delay alone already exceeds the throttle window). Dispatch
   it as a fire-and-forget task instead (_apply_action_logged).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fastapi import WebSocket, WebSocketDisconnect

from . import audio_state
from . import headsets as headsets_module
from . import pipewire
from . import views

logger = logging.getLogger("bragi.ws")

_THROTTLE_SECONDS = 0.1


class ConnectionManager:
    def __init__(self) -> None:
        self._queues: dict[WebSocket, asyncio.Queue] = {}

    def register(self, ws: WebSocket) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[ws] = queue
        return queue

    def unregister(self, ws: WebSocket) -> None:
        self._queues.pop(ws, None)

    def broadcast_nowait(self, message: dict) -> None:
        for queue in self._queues.values():
            queue.put_nowait(message)


manager = ConnectionManager()

# Per-(target, key, direction) single-flight coalescing, so a fast slider
# drag applies at most once per _THROTTLE_SECONDS - without this, every
# `input` event during a drag would spawn a wpctl/pw-cli subprocess.
#
# This MUST be single-flight (at most one apply+broadcast in flight per key
# at a time), not just "cancel the previous *pending* task" - an earlier
# design let a slow in-flight immediate-path apply (its own full ~150-250ms
# wpctl-call-then-broadcast round trip) keep running independently of newer
# messages, which get coalesced into a separate, faster trailing task. That
# faster task could finish and broadcast *before* the slow earlier one,
# so the earlier one's now-stale broadcast would arrive last and visibly
# drag the fader backward - confirmed live via a Playwright-driven drag,
# which is also why this needs its own test coverage (test_throttle_order
# below in spirit, see the repo's scratch tests) rather than trusting
# reasoning about it. A single worker per key, always applying only the
# latest queued value, makes stale-overwriting-fresh structurally
# impossible instead of just less likely.
_last_applied: dict[tuple, float] = {}
# Stores (ts, fn) together, not just fn - the worker broadcasts using THIS
# ts (the one the applied value actually corresponds to), never
# _max_ts_seen at broadcast time. Those can legitimately differ: _max_ts_seen
# is a running high-water mark that a *newer* message can bump the instant
# its own step-1 check runs, well before the worker gets around to applying
# whatever *older* (ts, fn) it's currently holding. Tagging with the
# high-water mark instead of the applied value's own ts was a real, subtly
# different bug from the two _max_ts_seen guards below: it let a broadcast
# carry an old value paired with a ts that looked fresh enough to pass the
# client's own check, still slipping an intermediate value through after a
# drag - confirmed live, it took a dedicated repro to catch since the
# adversarial ts-ordering stress test doesn't exercise broadcast tagging.
_pending_value: dict[tuple, tuple[float | None, Callable[[], None]]] = {}
_worker_running: set[tuple] = set()

# Per-(target, key, direction) high-water mark of the client's *own*
# timestamp (Date.now() from the browser tab that's dragging it) - never a
# server-generated one. The point of this whole mechanism is ordering
# messages from a single dragging client relative to each other; we're
# deliberately not solving for multiple clients moving the same control at
# once, so there's no server/client clock-skew concern to worry about -
# every value compared here originated from the same browser's Date.now().
#
# Two separate places need it:
# 1. apply_action calls _accept_ts() once, synchronously, in strict
#    message-receive order, before any resolve work - a message older
#    than the newest one already accepted for this control is dropped
#    immediately, without even paying for the resolve.
# 2. _throttled_apply re-checks it (read-only) right before writing into
#    _pending_value - because resolving a node id is a variable-duration
#    asyncio.to_thread call (a real pw-dump subprocess) that happens
#    *between* step 1 and this write, nothing guarantees those resolves
#    *complete* in the same order they were *submitted*. Without this
#    second check, an older message whose resolve happened to finish late
#    could still overwrite a newer message's already-queued value -
#    confirmed live with an adversarial-jitter stress test: step 1 alone
#    let the real applied volume regress mid-drag on 2 of 5 runs.
_max_ts_seen: dict[tuple, float] = {}


def _accept_ts(key: tuple, ts: float | None) -> bool:
    """Step 1 above. ts=None (some actions don't carry one, e.g.
    toggle_mute/toggle_enabled - see apply_action) always accepts, since
    there's nothing to compare against."""
    if ts is None:
        return True
    seen = _max_ts_seen.get(key)
    if seen is not None and seen >= ts:
        return False
    _max_ts_seen[key] = ts
    return True


def _apply_pan(node_id: int, node_name: str, volume: float | None = None, balance: float | None = None) -> None:
    """Applies a volume and/or balance change to a pannable node, always
    deriving "current volume"/"current balance" from audio_state's own
    store - never from wpctl, which only reports the FL channel and would
    be reading back whatever skew the last balance application left there.
    See audio_state.py's docstring for why that distinction is load-bearing."""
    cur_volume, cur_balance = audio_state.get_state(node_name)
    new_volume = cur_volume if volume is None else volume
    new_balance = cur_balance if balance is None else balance
    pipewire.set_channel_volumes(node_id, new_volume, new_balance)
    audio_state.set_state(node_name, new_volume, new_balance)


async def broadcast_control(target: str, key: str, direction: str, ts: float | None = None) -> None:
    """ts, when given, is the originating client's own Date.now() that
    caused this broadcast (see _max_ts_seen) - propagated through so the
    client can reject a broadcast that arrives out of order relative to
    one it's already shown, using its own clock throughout, never a
    server-generated one. Leave it None for anything that doesn't
    correspond to a specific client drag tick: a discrete toggle_mute
    click, or (importantly) the pw-mon watcher reacting to a hardware
    knob turn - that's genuinely new information uncorrelated with
    whatever _max_ts_seen happens to hold from a past drag, so it must
    never be compared against it, only always accepted."""
    fn = views.headset_control_view if target == "headset" else views.peer_control_view
    view = await asyncio.to_thread(fn, key, direction)
    if view is None:
        return
    manager.broadcast_nowait({"type": "control", "target": target, "key": key, "direction": direction, **view, "ts": ts})


async def broadcast_headset(key: str) -> None:
    """Whole-headset refresh - used for enable/disable, which isn't a
    per-direction control (it's a card.profile change, not a node volume),
    so broadcast_control's shape doesn't fit. Must resolve via
    get_headset_with_device (not views.get_headset), which is Node-only and
    would find nothing for a headset that's currently disabled - exactly
    the state this fires right after toggling into."""
    def _build():
        result = views.get_headset_with_device(key)
        if result is None:
            return None
        hs, device = result
        return views.headset_view(hs, device)

    view = await asyncio.to_thread(_build)
    if view is None:
        return
    manager.broadcast_nowait({"type": "headset", **view})


def _toggle_headset_enabled(card_id: str) -> None:
    device = headsets_module.get_headset_device(card_id)
    if device is None or device.off_profile_index is None:
        return
    currently_enabled = device.active_profile_index != device.off_profile_index
    if currently_enabled:
        pipewire.set_device_profile(device.id, device.off_profile_index)
    else:
        target_profile = device.restore_profile_index
        if target_profile is None:
            target_profile = device.off_profile_index
        pipewire.set_device_profile(device.id, target_profile)


async def _throttled_apply(key: tuple, fn: Callable[[], None], ts: float | None) -> None:
    """Records fn as the latest desired value for key and ensures exactly
    one worker is (or will shortly be) processing it. Never awaits the
    actual apply/broadcast itself - callers don't get backpressure from
    this, by design, since apply_action is already dispatched
    fire-and-forget (see websocket_endpoint).

    Re-checks _max_ts_seen (step 2 in its docstring) rather than trusting
    that apply_action's own earlier _accept_ts call (step 1) is still the
    last word - see that docstring for why both checks are required."""
    if ts is not None and _max_ts_seen.get(key, ts) > ts:
        return  # a newer message's step-1 check already ran while this one's resolve was in flight
    _pending_value[key] = (ts, fn)
    if key in _worker_running:
        return  # a worker is already active for this key - it will pick up this fn (or a newer one) itself
    _worker_running.add(key)
    asyncio.create_task(_throttle_worker(key))


async def _throttle_worker(key: tuple) -> None:
    """The single active worker for key. Loops as long as a newer value
    keeps arriving, always applying only the latest one - this is what
    makes it structurally impossible for a stale value to broadcast after
    a fresher one, unlike the previous design (see _pending_value's
    docstring above)."""
    try:
        while key in _pending_value:
            applied_ts, fn = _pending_value.pop(key)
            loop = asyncio.get_event_loop()
            now = loop.time()
            last = _last_applied.get(key, 0.0)
            wait = _THROTTLE_SECONDS - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
                if key in _pending_value:
                    applied_ts, fn = _pending_value.pop(key)  # take whatever arrived during the wait, not the stale one
            _last_applied[key] = asyncio.get_event_loop().time()
            await asyncio.to_thread(fn)
            await broadcast_control(*key, ts=applied_ts)
    finally:
        _worker_running.discard(key)
        # A value could have arrived in the tiny window between the while
        # loop's last check and this finally block - if so, restart.
        if key in _pending_value and key not in _worker_running:
            _worker_running.add(key)
            asyncio.create_task(_throttle_worker(key))


async def _apply_action_logged(action: dict) -> None:
    """Wraps apply_action for fire-and-forget dispatch (see
    websocket_endpoint) - exceptions would otherwise vanish into asyncio's
    generic "Task exception was never retrieved" warning instead of this
    logger, since nothing else ever awaits this task."""
    try:
        await apply_action(action)
    except Exception:
        logger.exception("failed to apply action: %r", action)


async def apply_action(action: dict) -> None:
    target = action.get("target")
    key = action.get("key")
    verb = action.get("action")
    direction = action.get("direction")

    if verb in ("set_volume", "set_balance") and not _accept_ts((target, key, direction), action.get("ts")):
        return

    if target == "headset":
        if verb == "toggle_enabled":
            # Resolved by device, not views.get_headset (Node-only) - a
            # disabled headset has no nodes at all, so the Node-based lookup
            # would find nothing and this could never re-enable anything.
            await asyncio.to_thread(_toggle_headset_enabled, key)
            await broadcast_headset(key)
            return

        # views.get_headset() shells out (pw-dump via list_headsets) - this
        # MUST be off the event loop thread. Running it inline here (as an
        # earlier version did) blocked the *entire* event loop for that
        # call's full ~75ms duration on every single message, which starved
        # everything else - the receive loop, the throttle worker's own
        # progress, broadcasts to other clients - regardless of how much
        # fire-and-forget dispatch or coalescing wraps around it. Confirmed
        # live via a Playwright-driven drag with the watcher fully disabled
        # as an A/B control (so it wasn't a watcher issue): the fader still
        # crawled through ~5 stale values after release, matching exactly
        # (20 messages * ~75ms blocked ≈ 1.5s) this cost multiplied out.
        def _resolve_headset_node() -> int | None:
            hs = views.get_headset(key)
            if hs is None:
                return None
            return hs.playback_node_id if direction == "playback" else hs.capture_node_id

        node_id = await asyncio.to_thread(_resolve_headset_node)
        if node_id is None:
            return
        if verb == "set_volume":
            await _throttled_apply(
                ("headset", key, direction),
                lambda nid=node_id, v=action["value"]: pipewire.set_volume(nid, v),
                action.get("ts"),
            )
        elif verb == "toggle_mute":
            def _toggle(nid=node_id):
                _, muted = pipewire.get_volume_mute(nid)
                pipewire.set_mute(nid, not muted)

            await asyncio.to_thread(_toggle)
            await broadcast_control("headset", key, direction)

    elif target == "peer":
        peer = views.get_peer(key)  # cheap - reads peers.yaml, no subprocess
        if peer is None:
            return

        # Same reasoning as _resolve_headset_node above: pipewire.list_nodes()
        # shells out, must not block the event loop.
        def _resolve_peer_node() -> int | None:
            nodes = pipewire.list_nodes()
            return views.resolve_node_id(nodes, peer, direction)

        node_id = await asyncio.to_thread(_resolve_peer_node)
        if node_id is None:
            return

        if verb == "set_volume" and direction == "incoming":
            node_name = views.peer_incoming_node_name(peer)
            await _throttled_apply(
                ("peer", key, "incoming"),
                lambda nid=node_id, nm=node_name, v=action["value"]: _apply_pan(nid, nm, volume=v),
                action.get("ts"),
            )
        elif verb == "set_volume":
            await _throttled_apply(
                ("peer", key, direction),
                lambda nid=node_id, v=action["value"]: pipewire.set_volume(nid, v),
                action.get("ts"),
            )
        elif verb == "toggle_mute":
            def _toggle(nid=node_id):
                _, muted = pipewire.get_volume_mute(nid)
                pipewire.set_mute(nid, not muted)

            await asyncio.to_thread(_toggle)
            await broadcast_control("peer", key, direction)
        elif verb == "set_balance" and direction == "incoming":
            node_name = views.peer_incoming_node_name(peer)
            await _throttled_apply(
                ("peer", key, "incoming"),
                lambda nid=node_id, nm=node_name, b=action["value"]: _apply_pan(nid, nm, balance=b),
                action.get("ts"),
            )


async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = manager.register(websocket)
    initial_state = await asyncio.to_thread(views.build_state)
    queue.put_nowait({"type": "state", **initial_state})

    receive_task: asyncio.Task | None = None
    queue_task: asyncio.Task | None = None
    try:
        while True:
            if receive_task is None:
                receive_task = asyncio.create_task(websocket.receive_json())
            if queue_task is None:
                queue_task = asyncio.create_task(queue.get())

            done, _pending = await asyncio.wait(
                {receive_task, queue_task}, return_when=asyncio.FIRST_COMPLETED
            )

            if receive_task in done:
                try:
                    action = receive_task.result()
                except WebSocketDisconnect:
                    break
                receive_task = None
                # Fire-and-forget, NOT awaited inline - see module docstring
                # point 3. Awaiting apply_action here would block this loop
                # from reading the *next* message until the current one's
                # real subprocess work (~150-250ms) finishes, which starves
                # _throttled_apply's own coalescing of any chance to run:
                # a fast drag (client sends every ~80ms) would then always
                # look "due" by the time each message is finally read,
                # since the delay of processing the previous one already
                # exceeds the throttle window. Confirmed live via a
                # Playwright-driven drag: the fader snapped back on release
                # and crawled through stale queued values for seconds.
                asyncio.create_task(_apply_action_logged(action))

            if queue_task in done:
                message = queue_task.result()
                queue_task = None
                # Only this task ever calls send_json - see module docstring.
                await websocket.send_json(message)
    finally:
        manager.unregister(websocket)
        for t in (receive_task, queue_task):
            if t is not None:
                t.cancel()


_WATCHER_SUPPRESS_SECONDS = 0.3

# node ids currently being resolved by on_node_changed - see its docstring.
_watcher_inflight: set[int] = set()


def _mark_applied(key: tuple) -> None:
    _last_applied[key] = asyncio.get_event_loop().time()


async def broadcast_headset_volume_change(key: str) -> None:
    """Called after the knob watcher applies a hardware-knob-driven volume
    step. Marks it in _last_applied the same way a throttled slider action
    would, so on_node_changed's suppression below also covers the knob
    path - it has the identical double-broadcast risk (pipewire.set_volume
    here also triggers a pw-mon event the watcher reacts to independently)."""
    _mark_applied(("headset", key, "playback"))
    await broadcast_control("headset", key, "playback")


async def on_node_changed(node_id: int) -> None:
    """watcher.py now dispatches this fire-and-forget per pw-mon event line,
    which during a fast drag (many real wpctl calls, often 2 pw-mon events
    each - the Node and its ALSA Device) means many concurrent calls for
    the same node_id can pile up. The in-flight guard collapses those to
    one resolve at a time per node_id - not just an efficiency nicety: it
    keeps the number of outstanding find_control_for_node calls bounded,
    which is what keeps the suppression check below actually current
    instead of racing a growing backlog (see watcher.py's docstring for the
    full failure mode this fixes, confirmed live via a Playwright drag)."""
    if node_id in _watcher_inflight:
        return
    _watcher_inflight.add(node_id)
    try:
        control = await asyncio.to_thread(views.find_control_for_node, node_id)
        if control is None:
            return
        # Every wpctl/pw-cli call Bragi itself makes also raises a pw-mon
        # "changed" event, which this watcher reacts to independently of
        # whatever action caused it - a client's own throttled slider tick
        # already gets a fresh, correct broadcast from _throttled_apply
        # itself, so this would just be a redundant echo, and not even a
        # reliably correct one: it can carry a subtly stale value (a
        # hardware read racing the write) or a differently-rounded one.
        # Skip it whenever a client action touched this exact control
        # recently enough that its own broadcast is still the freshest
        # information.
        last = _last_applied.get(control, 0.0)
        if asyncio.get_event_loop().time() - last < _WATCHER_SUPPRESS_SECONDS:
            return
        # Tag with the current high-water mark too (None if no client has
        # ever touched this control - a genuine hardware-only change, e.g.
        # an actual knob turn, which must always reach the client). This
        # closes a gap the suppression window above doesn't fully cover: a
        # delayed watcher event that slips past it during a fast drag was
        # otherwise broadcasting with no ts at all, bypassing the client's
        # own freshness check entirely (see ws.js's applyDirection) and
        # briefly showing a stale value even though the value here is
        # freshly read - confirmed live via a Playwright-driven drag.
        # Safe to attach: broadcast_control always re-reads current state
        # fresh regardless of which event triggered it, so a still-current
        # _max_ts_seen paired with a still-current read is never a lie,
        # even when the pw-mon event that triggered this call was stale.
        await broadcast_control(*control, ts=_max_ts_seen.get(control))
    finally:
        _watcher_inflight.discard(node_id)
