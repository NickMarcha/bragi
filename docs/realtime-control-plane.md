# The realtime control plane

How live updates actually work (`app/ws.py`, `app/watcher.py`,
`app/knob_watcher.py`, `app/static/ws.js`), and the debugging history behind
several non-obvious design choices in there. Read this before touching any
of those files - most of the odd-looking guards exist because a simpler
version of this code visibly glitched, confirmed by actually reproducing it
with a real browser drag, not by reasoning about the code.

**Status: believed correct, still under observation.** Every fix below was
verified with 8-15 consecutive real-browser drag reproductions against live
hardware on `sagepi`, with zero regressions in the final round. That's a
good sign, not a guarantee - several earlier "this looks fixed" rounds in
this same session turned out not to be, each time because the *next* round
of testing (not more code review) surfaced a further, subtler bug. Treat
"definitely fixed" with some skepticism until it's held up under real
day-to-day use, not just one session's testing.

## The design, end to end

1. **One WebSocket per browser tab** (`app/main.py`'s `/ws` route →
   `ws.websocket_endpoint`). The initial dashboard load is still plain
   server-rendered HTML; everything live after that goes over this socket.
2. **Every control (a headset direction, a peer direction) is identified by
   a `(target, key, direction)` tuple** - `("headset", card_id, "playback")`,
   `("peer", peer_name, "incoming")`, etc. This tuple is the key for all of
   the coalescing/ordering state below.
3. **A client action** (dragging a fader, dragging the balance pad, the
   right-click reset) sends `{action, target, key, direction, value, ts}`.
   `ts` is the *client's own* `Date.now()` (plus a tiny monotonic
   tie-breaker, see below) - never a server-generated timestamp.
4. **The server applies it via a single-flight-per-control worker**
   (`_throttled_apply` / `_throttle_worker` in `app/ws.py`), which:
   - Coalesces rapid messages for the same control down to roughly one
     real `wpctl`/`pw-cli` call per `_THROTTLE_SECONDS` (0.1s).
   - Guarantees only one real apply is ever in flight per control at a time.
   - Broadcasts a `{"type": "control", ...}` message back to *every*
     connected client after each real apply, tagged with the same `ts` the
     client sent for that specific value.
5. **The client rejects any incoming broadcast whose `ts` is older than the
   last message *that same client* sent for that control** (`lastSentTs` in
   `ws.js`), not older than the last broadcast it displayed. That
   distinction is the single most important thing in this file - see
   Bug 6 below for why.
6. **Two background watchers** react to changes Bragi didn't cause itself:
   - `app/watcher.py` runs `pw-mon -p` and reacts to any PipeWire prop
     change (another client, `pavucontrol`, or - the main case - a client's
     *own* action triggering a `pw-mon` event that has to be filtered back
     out; see Bug 4).
   - `app/knob_watcher.py` reads each headset's physical volume knob
     directly from its USB HID "Consumer Control" input device (these
     headsets send standard media keys, which nothing catches on a headless
     Pi - see the README) and applies real volume steps.

## Debugging history

Roughly chronological. Each entry: what it looked like, what it actually
was, how it was actually confirmed (not just reasoned about).

### 1. Sequential `await` in the receive loop
**Looked like:** the fader snapped back on release and crawled through
stale values for seconds.
**Was:** `websocket_endpoint`'s receive loop did `await apply_action(...)`
inline, so it couldn't read the *next* message until the current one's real
subprocess work (~150-250ms) finished. A fast drag (client sends every
~80ms) always looked "due" by the time each message was finally read, so
the throttle logic never got a chance to coalesce anything.
**Fix:** dispatch `apply_action` via `asyncio.create_task` (fire-and-forget,
with its own exception logging - `_apply_action_logged`).

### 2. A slow "immediate" apply racing a fast "trailing" one
**Looked like:** still crawling, even after fix 1.
**Was:** the original throttle design cancelled *pending* (trailing) tasks
on a new message, but a message that had already taken the "apply
immediately" path kept running independently. If that immediate apply was
slow and several newer messages got coalesced into a separate, faster
trailing task, the fast one could finish and broadcast *before* the slow
one - so the slow one's now-stale broadcast arrived last.
**Fix:** rebuilt as a true single-flight worker (`_throttle_worker`) - one
task per control, looping and always grabbing the *current* latest pending
value, so there is structurally only ever one real apply in flight.
**Confirmed via:** `test_single_flight.py`-style test with artificial jitter
in the fake apply function, asserting applies never overlap and are never
out of order.

### 3. A blocking subprocess call on the event loop
**Looked like:** still crawling, even after fix 2 - narrowed down by
disabling the `pw-mon` watcher entirely as an A/B control and confirming the
crawl was unaffected, ruling it out as the cause.
**Was:** `views.get_headset(key)` (called on *every* message to resolve a
node id) shelled out to `pw-dump` synchronously, directly on the event loop
thread - not wrapped in `asyncio.to_thread`. That blocked the *entire*
event loop, including the worker's own progress and the receive loop, for
~75ms per message. Twenty messages in a drag ≈ 1.5s of pure blocking.
**Fix:** wrap every such call (`views.get_headset`, `pipewire.dump`,
the `toggle_mute` handlers) in `asyncio.to_thread`.

### 4. The watcher backlogging on its own event stream
**Looked like:** the crawl was gone for volume, but a *different* delayed
crawl still showed up, tied to the pw-mon watcher.
**Was:** `watcher.py`'s line-reading loop `await`ed the `on_change` callback
per pw-mon event line. A fast drag causes many real `wpctl` calls, each
producing 1-2 pw-mon events (the Node and its ALSA Device) - the watcher's
own loop fell behind the same way the original receive loop did (Bug 1),
and kept draining that backlog for seconds after the drag ended, each
backlogged event by then falling outside the "was this just caused by a
client?" suppression window and broadcasting a stale value.
**Fix:** dispatch `on_change` fire-and-forget from the watcher's read loop
too, plus an in-flight guard per `node_id` in `ws.on_node_changed` so a
burst of duplicate events for the same node collapses to one resolve.

**Superseded by bug 7.** The per-`node_id` guard only ever deduplicated
*repeat* events for the *same* node. It did nothing for a burst spread
across *many* nodes, which is the shape a profile change actually takes.

### 5. The apply-side race, closed with client timestamps
**Looked like:** an occasional (not every drag) regression even with fixes
1-4 in place.
**Was:** even with fire-and-forget dispatch, each message's node-id
resolve is a variable-duration `asyncio.to_thread` call. Nothing guarantees
those *complete* in the same order they were *submitted* - a later
message's resolve can finish first and write into the pending-value slot
before an earlier message's resolve does.
**Fix:** the client sends its own `ts` with every `set_volume`/`set_balance`.
The server checks it twice: once immediately on receipt (`_accept_ts`, in
strict receive order, before any resolve work - cheap, rejects obviously
stale messages early) and again right before the actual write into the
pending-value slot (`_throttled_apply`, re-checking `_max_ts_seen` - because
step one's check being in order doesn't mean it's still the last word by
the time the resolve finishes).
**Confirmed via:** `test_ts_rejection.py`, which injects *random* jitter
into the fake resolve step specifically to provoke out-of-order completion.
Without the fix this failed roughly half the time; with it, 15/15 clean.

Two follow-on bugs surfaced *by that same test* while building it:
- **Millisecond ties**: `Date.now()` has only ms resolution, and the
  server's check rejects ties (`seen >= ts`), not just older values - two
  messages landing in the same millisecond could see the second one wrongly
  dropped. Fixed with a monotonic fractional tie-breaker in `ws.js`
  (`nextTs()`).
- **Stale value paired with a fresh-looking timestamp**: the worker
  originally tagged its broadcast with `_max_ts_seen.get(key)` (the *current*
  high-water mark) rather than the timestamp the *specific value it just
  applied* actually corresponds to. A newer message can bump that high-water
  mark while the worker is still catching up on an older one, so the
  broadcast would carry an old value paired with a timestamp that looked
  fresh enough to pass every check. Fixed by storing `(ts, fn)` together in
  the pending slot and broadcasting with that value's own `ts`, not the
  running max.

### 6. "Last shown" vs. "last sent" - the one the user caught
**Looked like:** noticeably better after fix 5, but still an occasional
visible jump backward right after releasing a drag, followed by a
self-correction.
**Was:** the client's staleness check compared an incoming broadcast's `ts`
against the `ts` of the *last broadcast it had displayed* - but broadcasts
keep arriving throughout a fast drag, and the dragging-flag guard correctly
skips *displaying* most of them while dragging, while still recording their
`ts` as "seen". That's a much lower bar than "caught up to my true final
position": the moment you release, the *next* arriving broadcast only had
to beat that stale bookmark, not your actual last input, so it displayed
some intermediate value before the real final one eventually arrived.
**Fix:** track `lastSentTs` - the `ts` of the last message *this client*
sent for that control - and reject any incoming broadcast older than that,
full stop, regardless of what's been displayed in the meantime. This was
the user's own proposed design or the exact next step; it's not a
project-generic replacement for the ordering fixes above, because a
watcher-triggered broadcast (a real hardware knob turn) has no client `ts`
at all - that case still just always passes through.
**Confirmed via:** 12 consecutive live drag reproductions with zero visible
jumps in any of them, versus roughly 1-in-4 showing a brief self-correcting
blip immediately beforehand.

### 7. One click, 76 `pw-dump` calls
**Looked like:** enable/disable took whole seconds - measured over the live
WebSocket at **9.7s** from click to the card updating, against ~0.5s for
every other control.
**Was:** three separate causes stacked on one click.
1. A single ALSA profile flip emits **147 pw-mon "changed" events across 72
   distinct object ids** (ports and links, mostly). `on_node_changed` then
   resolved each id independently, and each resolve ran its *own*
   `pw-dump` - the fix-4 guard deduplicated only repeats of the same id,
   which a burst like this barely contains. Measured against the fake:
   **76 dumps for one click.**
2. `list_nodes()` and `list_devices()` each ran their own `pw-dump`, even
   though one dump already carries both. The enable/disable path paid for
   three: one to find the Device, two more to rebuild the view.
3. All of it ran on a box the level meters already kept at ~75% load.

At ~100ms per dump (85ms to run, 17ms to parse 495KB of JSON on the Pi 4),
(1) alone is ~7.6 seconds of subprocess work, queued 8-at-a-time through
`asyncio.to_thread`'s default executor and saturating all four cores -
which is also why the click's own critical path got starved.

**Fix:** `pipewire.dump()` returns a `Graph` carrying nodes *and* devices
from one call, passed down explicitly instead of re-fetched; and
`on_node_changed` now only records the id, with `_drain_changed_nodes`
waiting out the burst (`_WATCHER_COALESCE_SECONDS`, 150ms) and resolving
the whole set against a single graph via `views.find_controls_for_nodes`.
72 ids collapse to the one or two controls they actually represent.
**Confirmed via:** 76 dumps → **1**; 9.7s → **0.55s** over the socket, and
**798ms** from a real Chrome click to the card visibly updating.

### 8. The state snapshot arriving behind the meters
**Looked like:** a newly connected tab's first WebSocket message was a
level frame, not its state snapshot. Surfaced immediately by a probe script
that read the first message and found no `headsets` key in it.
**Was:** `websocket_endpoint` registered the connection's queue, *then*
built the snapshot (~600ms of `pw-dump` and `wpctl` on a Pi 4) and queued
it. Anything broadcast during that window - and registering now also wakes
the level meters, so frames reliably land there - queued *ahead* of it.
Harmless for a level frame; not harmless for a control broadcast, which the
late-arriving snapshot would then overwrite with its own older read.
**Fix:** still register first, so nothing broadcast during the build is
lost, but send the snapshot with a direct `await websocket.send_json(...)`
before entering the receive/queue loop. See the `send_json` note below for
why that one direct send is safe.

## Practical notes for future changes

- If you touch `_throttled_apply`/`_throttle_worker`/`_accept_ts`, re-run
  `pytest` before trusting it - reasoning about ordering here has a proven
  track record of missing things. `tests/test_dashboard_state.py` covers
  timestamp rejection; the jitter stress test described in fix 5 is still
  not checked in, so for changes in that area write one fresh that injects
  artificial jitter into a faked resolve step and asserts both "never
  concurrent" and "never out of order", not just "ends up correct
  eventually".
- Assert subprocess *count*, not just behaviour. Every slowness bug in this
  file was a subprocess-count bug, and `tests/fake_pipewire.py`'s
  `session.count("pw-dump")` is the cheapest possible guard against the
  next one.
- Test drag stability with an actual browser driving real pointer events
  (Playwright or similar) against the real deployment, not just synthetic
  WebSocket messages - several of the bugs above only showed up under real
  timing (throttle intervals, resolve latency, network round trips), not in
  a mocked unit test sending messages in a tight loop.
- Never broadcast the *whole* dashboard state after a single action -
  `views.build_state()` is ~11 sequential subprocess calls, ~625ms on a
  Pi 4. Every targeted broadcast must go through
  `views.headset_control_view`/`peer_control_view` (1-2 calls).
- Never let more than one coroutine call `websocket.send_json()` on the
  same connection. Route every broadcast source through
  `ConnectionManager.broadcast_nowait`, which only ever enqueues onto each
  connection's own `asyncio.Queue` - only the connection's own task (inside
  `websocket_endpoint`) actually calls `send_json`. The single exception is
  the initial snapshot (fix 8), sent directly *before* the receive/queue
  loop starts, when nothing else can possibly be touching the socket yet.
  An earlier version let
  background tasks call `send_json` directly, which corrupted Starlette's
  connection state and crashed with a "WebSocket is not connected" error on
  the next unrelated `receive_json()` call - a very confusing failure mode
  for what was actually a concurrency bug three call-frames away.
