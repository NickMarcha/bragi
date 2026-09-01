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
from . import viz_settings

logger = logging.getLogger("bragi.ws")

_THROTTLE_SECONDS = 0.1

# How many level frames may sit unread in one connection's queue before
# further ones are dropped for it - see broadcast_levels_nowait. Small
# on purpose: at 20 frames a second this is well under a second of
# meter history, which is all a live meter is worth.
MAX_QUEUED_LEVEL_FRAMES = 4


class ConnectionManager:
    def __init__(self) -> None:
        self._queues: dict[WebSocket, asyncio.Queue] = {}
        # Set whenever something changes what should be metered - a tab
        # connecting or closing, or the viz toggle being flipped.
        # level_meter.supervise() waits on this instead of only its own
        # timer, so opening the dashboard starts the meters immediately
        # rather than up to _RECONCILE_SECONDS later. Owned here rather
        # than in level_meter because level_meter already imports ws, and
        # the reverse would be a cycle.
        self.wake = asyncio.Event()

    def register(self, ws: WebSocket) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[ws] = queue
        self.wake.set()
        return queue

    def unregister(self, ws: WebSocket) -> None:
        self._queues.pop(ws, None)
        self.wake.set()

    def has_clients(self) -> bool:
        """Used by level_meter.py's supervisor to decide whether any node
        capture should run at all - no point spawning pw-cat processes for
        a dashboard nobody has open."""
        return bool(self._queues)

    def broadcast_nowait(self, message: dict) -> None:
        for queue in self._queues.values():
            queue.put_nowait(message)

    def broadcast_levels_nowait(self, message: dict) -> None:
        """Like broadcast_nowait, but drops the frame for any connection
        that is already behind.

        Level frames are only worth anything while they are current, and
        they arrive 20 times a second for as long as a tab is open. This
        queue only grows when the connection itself applies backpressure -
        `send_json` blocking on a full write buffer, i.e. a client too slow
        to take what it is being sent - but when that happens there is no
        bound on it at all, and every frame it accumulates is one the client
        will eventually be shown *late*, fast-forwarding the meters through
        history instead of showing the present.

        Note this bounds the *server* side only. A client that simply stops
        calling recv() backs up in its own OS receive buffer, which nothing
        here can see or do anything about.

        Control messages deliberately do NOT get this treatment - nothing
        re-sends them, so a dropped one leaves a fader wrong indefinitely.
        A backlog of those is a real problem to fix at the source, not to
        paper over here."""
        for queue in self._queues.values():
            if queue.qsize() < MAX_QUEUED_LEVEL_FRAMES:
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
    def _build() -> dict | None:
        graph = pipewire.dump()
        fn = views.headset_control_view if target == "headset" else views.peer_control_view
        return fn(graph, key, direction)

    view = await asyncio.to_thread(_build)
    if view is None:
        return
    manager.broadcast_nowait({"type": "control", "target": target, "key": key, "direction": direction, **view, "ts": ts})


async def broadcast_controls(controls: set[tuple[str, str, str]], graph: pipewire.Graph) -> None:
    """Broadcasts several controls off one already-taken graph - the batch
    counterpart to broadcast_control, used by the watcher (see
    _drain_changed_nodes) so a burst of pw-mon events costs one pw-dump
    total rather than one per control."""
    for target, key, direction in sorted(controls):
        fn = views.headset_control_view if target == "headset" else views.peer_control_view
        view = await asyncio.to_thread(fn, graph, key, direction)
        if view is None:
            continue
        manager.broadcast_nowait(
            {
                "type": "control",
                "target": target,
                "key": key,
                "direction": direction,
                **view,
                "ts": _max_ts_seen.get((target, key, direction)),
            }
        )


def _toggle_headset_enabled(card_id: str) -> dict | None:
    """Flips the card's ALSA profile and builds the resulting headset view
    off a single graph. Enable and the view that follows it are one unit
    on purpose: the whole click used to cost three pw-dumps (~100ms each on
    a Pi 4), one to find the Device and two more to rebuild the view."""
    graph = pipewire.dump()
    device = headsets_module.get_headset_device(graph, card_id)
    if device is None or device.off_profile_index is None:
        return None
    currently_enabled = device.active_profile_index != device.off_profile_index
    if currently_enabled:
        target_profile = device.off_profile_index
    else:
        # None means no usable profile to restore, so the card is left alone
        # rather than having "off" written over it. A USB card re-enumerates
        # its profiles as it comes and goes, so this window is real, and the
        # old fallback made a click asking to *enable* the headset silently
        # disable it again and report it as off: a button that visibly does
        # nothing.
        target_profile = device.restore_profile_index

    if target_profile is not None:
        pipewire.set_device_profile(device.id, target_profile)
        device.active_profile_index = target_profile

    # The card's own nodes appear or disappear with the profile flip, so the
    # graph just dumped is already stale for the *nodes*. Patching the
    # Device's own enabled state and reusing it is still right for what this
    # view shows: which directions exist is derived from the nodes, and
    # those settle asynchronously anyway - the watcher's own coalesced pass
    # delivers the settled volumes a moment later (see _drain_changed_nodes).
    hs = views.get_headset(graph, card_id)
    if hs is None:
        return None
    return views.headset_view(hs, device)


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

    if verb == "set_viz_enabled":
        # No target/key/direction - a single global toggle, not a per-node
        # control, see viz_settings.py.
        enabled = bool(action.get("value"))
        await asyncio.to_thread(viz_settings.set_enabled, enabled)
        manager.broadcast_nowait({"type": "viz_settings", "enabled": enabled})
        manager.wake.set()  # start or stop capturing now, not on the next reconcile
        return

    if target == "headset":
        if verb == "toggle_enabled":
            # Resolved by ALSA card Device, never by node - a disabled
            # headset has no nodes at all, so a Node-based lookup would find
            # nothing and this could never re-enable anything.
            view = await asyncio.to_thread(_toggle_headset_enabled, key)
            if view is not None:
                manager.broadcast_nowait({"type": "headset", **view})
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
            hs = views.get_headset(pipewire.dump(), key)
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

        # Same reasoning as _resolve_headset_node above: pipewire.dump()
        # shells out, must not block the event loop.
        def _resolve_peer_node() -> int | None:
            return views.resolve_node_id(pipewire.dump(), peer, direction)

        node_id = await asyncio.to_thread(_resolve_peer_node)
        if node_id is None:
            return

        # Both peer directions are software Roc/VBAN stream nodes (unlike
        # headset directions, which are real ALSA hardware - see
        # direction_view's docstring), so both are pannable.
        pan_node_name = None
        if direction == "incoming":
            pan_node_name = views.peer_incoming_node_name(peer)
        elif direction == "outgoing":
            pan_node_name = views.peer_outgoing_node_name(peer)

        if verb == "set_volume" and pan_node_name:
            await _throttled_apply(
                ("peer", key, direction),
                lambda nid=node_id, nm=pan_node_name, v=action["value"]: _apply_pan(nid, nm, volume=v),
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
        elif verb == "set_balance" and pan_node_name:
            await _throttled_apply(
                ("peer", key, direction),
                lambda nid=node_id, nm=pan_node_name, b=action["value"]: _apply_pan(nid, nm, balance=b),
                action.get("ts"),
            )


async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = manager.register(websocket)

    # Registered before the snapshot is built, so nothing broadcast during
    # the build is lost - but sent directly rather than queued, so the
    # snapshot is still the first thing this tab sees. build_state() takes
    # ~600ms of pw-dump and wpctl on a Pi 4, and registering also wakes the
    # level meters, so frames reliably land in this queue while it runs.
    # Queueing the snapshot behind them let a control broadcast arrive
    # before the state it belongs to, and the snapshot would then overwrite
    # that fresher value with its own older read.
    #
    # Sending directly is safe only here: the receive/queue loop below has
    # not started yet, so this is still the only thing touching the socket
    # (see this module's docstring, point 1).
    initial_state = await asyncio.to_thread(views.build_state)
    await websocket.send_json({"type": "state", **initial_state})

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

# How long on_node_changed collects ids before resolving them as one batch.
# Long enough to swallow a profile flip's whole event storm (measured on
# sagepi: 147 events across 72 ids, all inside ~100ms), short enough that a
# physical knob turn still feels immediate.
_WATCHER_COALESCE_SECONDS = 0.15

# Ids seen from pw-mon but not yet resolved, and the single task that will
# drain them - see on_node_changed.
_changed_nodes: set[int] = set()
_drain_task: asyncio.Task | None = None


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
    """Records one pw-mon "changed" id and makes sure a drain is scheduled.

    Cheap and synchronous on purpose. watcher.py dispatches this
    fire-and-forget per event line, and the events arrive in bursts far
    bigger than the number of controls they represent: one headset profile
    flip emits 147 events across 72 distinct ids (measured on sagepi), and a
    fast fader drag emits two per wpctl call. The previous version resolved
    each id independently, each paying its own pw-dump - ~7s of subprocess
    work for a single click, which is most of why enable/disable took 9.7s.
    Collecting ids and resolving the whole set at once collapses that to one
    dump per burst."""
    _changed_nodes.add(node_id)
    global _drain_task
    if _drain_task is None or _drain_task.done():
        _drain_task = asyncio.create_task(_drain_changed_nodes())


async def _drain_changed_nodes() -> None:
    """Waits out the burst, then resolves every id collected during it
    against one graph and broadcasts the (usually one or two) controls they
    map to."""
    await asyncio.sleep(_WATCHER_COALESCE_SECONDS)
    node_ids = set(_changed_nodes)
    _changed_nodes.clear()
    if not node_ids:
        return

    def _resolve() -> tuple[pipewire.Graph, set[tuple[str, str, str]]]:
        graph = pipewire.dump()
        return graph, views.find_controls_for_nodes(graph, node_ids)

    graph, controls = await asyncio.to_thread(_resolve)

    # Every wpctl/pw-cli call Bragi itself makes also raises a pw-mon
    # "changed" event, which this watcher reacts to independently of
    # whatever action caused it - a client's own throttled slider tick
    # already gets a fresh, correct broadcast from _throttled_apply itself,
    # so this would just be a redundant echo, and not even a reliably
    # correct one: it can carry a subtly stale value (a hardware read racing
    # the write) or a differently-rounded one. Skip any control a client
    # action touched recently enough that its own broadcast is still the
    # freshest information.
    now = asyncio.get_event_loop().time()
    controls = {c for c in controls if now - _last_applied.get(c, 0.0) >= _WATCHER_SUPPRESS_SECONDS}

    # broadcast_controls tags each with the current high-water mark (None if
    # no client has ever touched that control - a genuine hardware-only
    # change, e.g. an actual knob turn, which must always reach the client).
    # This closes a gap the suppression window above doesn't fully cover: a
    # delayed watcher event that slips past it during a fast drag was
    # otherwise broadcasting with no ts at all, bypassing the client's own
    # freshness check entirely (see ws.js's applyDirection) and briefly
    # showing a stale value even though the value here is freshly read -
    # confirmed live via a Playwright-driven drag. Safe to attach: the view
    # is re-read fresh regardless of which event triggered it, so a
    # still-current _max_ts_seen paired with a still-current read is never a
    # lie, even when the pw-mon event was stale.
    await broadcast_controls(controls, graph)

    # Ids that arrived while the resolve was in flight get their own pass.
    if _changed_nodes:
        global _drain_task
        _drain_task = asyncio.create_task(_drain_changed_nodes())
