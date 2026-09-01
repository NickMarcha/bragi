"""Live audio level (VU-style) metering for headset and peer strips.

Nothing else in this codebase can answer "how loud is it right now" -
wpctl/pw-dump only ever report the *volume setting* (see pipewire.py), never
the actual signal. Getting a real level means tapping the audio stream
itself, via `pw-cat --record` targeting a node (confirmed live on sage-dev:
`--target <node.name-or-id> --channels 1 --format s16 --rate 8000 --raw`
streams clean headerless raw PCM, byte count matches the format math
exactly - no WAV header, no surprises).

Deliberately its own module, not part of ws.py: same reasoning
peer_presence.py's docstring already gives for keeping unrelated concerns
out of that file.

Two independent gates decide whether any capture runs at all, both checked
by supervise() on every reconcile pass, never inside the per-node capture
loop itself:
1. viz_settings.get_enabled() - the user-facing off switch. This is a real
   resource/privacy control, not just a display toggle: off means zero
   mic/speaker capture happens, full stop, regardless of who's connected.
2. ws.manager.has_clients() - even when enabled, no point capturing for a
   dashboard nobody has open.

Headsets are targeted by node *name* (stable - headsets_module already
tracks this). Peers are targeted by node *id*, not the synthetic
peer_incoming_node_name/peer_outgoing_node_name views.py uses for
audio_state keys - those aren't real PipeWire node names. This matters
concretely for VBAN: both its directions are literally named "vban" (see
views.resolve_node_id), so targeting by name would be ambiguous - the id
views.resolve_node_id already resolves (with its own media-class
disambiguation) is the only unambiguous handle. Ids are ephemeral, but
_desired_nodes() re-resolves from scratch every reconcile pass anyway, so a
churned id just looks like "target changed" and gets a fresh capture
started - the same tolerance the rest of this codebase already has for
node ids being reassigned on reload.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import struct

from . import headsets as headsets_module
from . import peers as peers_module
from . import pipewire
from . import views
from . import viz_settings
from . import ws

logger = logging.getLogger("bragi.level_meter")

_SAMPLE_RATE = 8000  # envelope metering only, not fidelity - keeps pw-cat's
                      # own CPU/bandwidth footprint down versus 48kHz
_CHUNK_SECONDS = 0.05  # one level update per chunk => ~20Hz broadcast rate
_CHUNK_BYTES = int(_SAMPLE_RATE * _CHUNK_SECONDS) * 2  # s16 mono = 2B/sample
_RECONCILE_SECONDS = 2.0

# Standard VU-style ballistics: jump up almost immediately, decay slowly, so
# the meter doesn't look twitchy. Expressed as time constants (tau) and
# converted to a per-chunk exponential-smoothing coefficient so retuning
# either number doesn't require re-deriving the math by hand.
_ATTACK_TAU = 0.02
_RELEASE_TAU = 0.4
_ATTACK_COEFF = 1 - math.exp(-_CHUNK_SECONDS / _ATTACK_TAU)
_RELEASE_COEFF = 1 - math.exp(-_CHUNK_SECONDS / _RELEASE_TAU)

# The displayed value is a dB-mapped 0..1, not raw linear RMS - confirmed
# live this matters, not just theoretical: an ambient room's mic noise
# floor measured ~0.0008 linear (~-62dBFS), and normal speech rarely
# approaches 1.0 linear (near-clipping) - a linear meter reads as "not
# moving" for anything short of very loud audio. -50dBFS as the floor
# means a quiet room correctly reads ~0 while normal speech (roughly
# -20 to -10dBFS) lands in a clearly visible 60-80% range.
_MIN_DB = -50.0


def _to_display_value(level: float) -> float:
    if level <= 0:
        return 0.0
    db = 20 * math.log10(level)
    return max(0.0, min(1.0, (db - _MIN_DB) / (0 - _MIN_DB)))


def _desired_nodes() -> dict[tuple[str, str, str], str]:
    """(target, key, direction) -> pw-cat --target value (node name for
    headsets, node id-as-string for peers - see module docstring) for
    every direction that should currently be metered. Empty whenever
    either gate above is closed - supervise() diffs this against what's
    actually running."""
    if not viz_settings.get_enabled() or not ws.manager.has_clients():
        return {}
    graph = pipewire.dump()
    desired: dict[tuple[str, str, str], str] = {}
    for hs in headsets_module.list_headsets(graph):
        if hs.playback_node_name:
            desired[("headset", hs.key, "playback")] = hs.playback_node_name
        if hs.capture_node_name:
            desired[("headset", hs.key, "capture")] = hs.capture_node_name
    for peer in peers_module.load_peers():
        for direction in ("outgoing", "incoming"):
            node_id = views.resolve_node_id(graph, peer, direction)
            if node_id is not None:
                desired[("peer", peer.name, direction)] = str(node_id)
    return desired


# The latest display value per metered direction, and the ones that have
# just stopped and still owe the client a final zero. Readers write here
# rather than broadcasting; one ticker (broadcast_levels_once) turns the
# whole set into a single WebSocket frame - see its docstring.
_levels: dict[tuple[str, str, str], float] = {}
_stopped: set[tuple[str, str, str]] = set()


def report_level(control: tuple[str, str, str], value: float) -> None:
    _levels[control] = value
    _stopped.discard(control)


def stop_level(control: tuple[str, str, str]) -> None:
    """A capture ended (headset disabled or unplugged, peer removed, viz
    switched off). Queues one final zero: without it the client keeps the
    bar painted at whatever height the last frame left it, so a disabled
    headset appears to be sitting at a constant level forever."""
    if control in _levels:
        _levels[control] = 0.0
        _stopped.add(control)


def current_levels() -> dict[tuple[str, str, str], float]:
    return dict(_levels)


async def broadcast_levels_once() -> None:
    """One frame for every meter, instead of one frame per meter.

    8 metered directions at 20Hz was 160 WebSocket frames a second for a
    single dashboard, which cost more in framing, tailnet encryption and
    client-side JSON parsing than the levels themselves are worth - it
    measured as 39% CPU in tailscaled on sagepi. The same information fits
    in 20 frames a second, unchanged in content or rate of update."""
    if not _levels:
        return
    ws.manager.broadcast_levels_nowait(
        {
            "type": "levels",
            "values": [
                {"target": t, "key": k, "direction": d, "value": v}
                for (t, k, d), v in sorted(_levels.items())
            ],
        }
    )
    for control in _stopped:
        _levels.pop(control, None)
    _stopped.clear()


async def _ticker() -> None:
    while True:
        await asyncio.sleep(_CHUNK_SECONDS)
        await broadcast_levels_once()


async def _read_levels(stream, target: str, key: str, direction: str) -> None:
    """Turns one capture's raw PCM into a smoothed display level. Takes the
    stream rather than the Process so the arithmetic can be exercised
    against a scripted signal."""
    control = (target, key, direction)
    level = 0.0
    while True:
        chunk = await stream.readexactly(_CHUNK_BYTES)
        n = len(chunk) // 2
        samples = struct.unpack(f"<{n}h", chunk)  # s16 is native, but every
        # box this runs on (sage-dev x86_64, sagepi arm64) is little-endian -
        # forcing '<' here makes that explicit instead of assumed.
        rms = math.sqrt(sum(s * s for s in samples) / n) / 32768.0 if n else 0.0
        coeff = _ATTACK_COEFF if rms > level else _RELEASE_COEFF
        level += (rms - level) * coeff
        report_level(control, _to_display_value(level))


async def _monitor_node(target: str, key: str, direction: str, pw_target: str) -> None:
    """Runs forever (until cancelled by supervise()'s reconcile loop),
    restarting pw-cat if it exits unexpectedly - same restart-on-crash
    shape as watcher.py's pw-mon loop."""
    while True:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "pw-cat", "--record", "-",
                "--target", pw_target,
                "--channels", "1",
                "--format", "s16",
                "--rate", str(_SAMPLE_RATE),
                "--raw",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdout is not None
            await _read_levels(proc.stdout, target, key, direction)
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.terminate()
                with contextlib.suppress(Exception):
                    await proc.wait()
            stop_level((target, key, direction))
            raise
        except (asyncio.IncompleteReadError, Exception):
            # Covers a missing pw-cat binary, the target node disappearing
            # mid-capture (headset unplugged/disabled, peer removed), or a
            # normal process exit - supervise() will stop retrying once the
            # node is no longer in the desired set; until then, keep trying.
            logger.exception("level meter for %s/%s/%s crashed, restarting", target, key, direction)
        await asyncio.sleep(1)


async def supervise() -> None:
    """Runs forever as a lifespan background task - reconciles the desired
    node set against currently-running capture tasks every
    _RECONCILE_SECONDS, starting new ones and cancelling stale ones. This
    single reconcile loop covers every way the desired set can change (a
    client connecting/disconnecting, a headset/peer being added, enabled,
    disabled, unplugged, or removed, the viz setting being flipped) without
    needing a hook at every one of those call sites."""
    running: dict[tuple[str, str, str], tuple[str, asyncio.Task]] = {}
    ticker = asyncio.create_task(_ticker())
    try:
        while True:
            # Cleared before reading the desired set, not after reconciling,
            # so a change that lands *during* a reconcile still triggers the
            # next one instead of being swallowed.
            ws.manager.wake.clear()
            try:
                desired = await asyncio.to_thread(_desired_nodes)
            except Exception:
                logger.exception("failed to resolve desired level-meter nodes")
                desired = {}

            for key in list(running):
                pw_target, task = running[key]
                if desired.get(key) != pw_target:
                    task.cancel()
                    stop_level(key)
                    del running[key]

            for key, pw_target in desired.items():
                if key not in running:
                    task = asyncio.create_task(_monitor_node(key[0], key[1], key[2], pw_target))
                    running[key] = (pw_target, task)

            # The timer is the floor, not the only trigger: a tab opening had
            # to wait it out before any pw-cat was even spawned, which (plus
            # pw-cat's own ~0.5-1s to first byte) left the meters dead for
            # about three seconds every time the dashboard was opened.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ws.manager.wake.wait(), timeout=_RECONCILE_SECONDS)
    finally:
        ticker.cancel()
        for _, task in running.values():
            task.cancel()
        for _, task in running.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
