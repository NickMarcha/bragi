"""Background task that watches PipeWire for changes we didn't cause
ourselves - the only source of those is the physical volume knob on a
headset (or someone using pavucontrol/wpctl directly on the host).

Runs `pw-mon -p` (the -p flag exists specifically "to help a streaming
parser", per its own --help) as a long-lived subprocess and reads it line
by line. `pw-mon` dumps the *entire* current param/prop state of an object
on every "changed:" event, which is too verbose and schema-heavy to parse
for the actual value - so this only extracts the changed object's id and
hands it to a callback, which re-checks that one node the same way a user
action would (pipewire.get_volume_mute), rather than trying to parse a
volume out of pw-mon's dump directly.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from . import pipewire

logger = logging.getLogger("bragi.watcher")

_ID_RE = re.compile(r"^\tid:\s*(\d+)")
_EVENTS = ("added:", "removed:", "changed:")

OnChange = Callable[[int], Awaitable[None]]


async def _read_events(proc: asyncio.subprocess.Process, on_change: OnChange) -> None:
    """Dispatches on_change fire-and-forget (asyncio.create_task), never
    awaited inline - the same class of bug as an earlier one already fixed
    in ws.py's websocket_endpoint, confirmed live via the same method
    (a Playwright-driven drag): pw-mon emits a "changed:" event for every
    real wpctl call, often two per call (the Node and its ALSA Device), so
    a fast drag can produce dozens of lines. Awaiting on_change per line
    made this loop fall behind the actual event stream, and it kept
    draining that backlog for seconds *after* the drag ended - by which
    point the corresponding _last_applied timestamp in ws.py's suppression
    check had gone stale, so each backlogged event broadcast an
    increasingly-outdated value, visibly dragging the fader backward then
    crawling it back up. on_node_changed has its own single-flight guard
    per node_id to avoid piling up redundant concurrent resolves from this
    now-unblocked dispatch."""
    assert proc.stdout is not None
    event = None
    async for raw_line in proc.stdout:
        line = raw_line.decode(errors="replace").rstrip("\n")
        if line in _EVENTS:
            event = line
            continue
        if event is not None:
            m = _ID_RE.match(line)
            if m:
                node_id = int(m.group(1))
                # Any of the three can mean pipewire.py's cached volume for
                # this id is wrong - removed/added included, since PipeWire
                # reuses a destroyed object's id. Only "changed" is a change
                # worth showing a dashboard tab, though.
                pipewire.forget_volume(node_id)
                if event == "changed:":
                    asyncio.create_task(on_change(node_id))
            event = None


async def watch(on_change: OnChange) -> None:
    """Runs forever, restarting `pw-mon` if it exits unexpectedly (e.g. the
    PipeWire daemon restarts). Intended to be launched as a background
    asyncio task on FastAPI startup, not awaited to completion."""
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pw-mon", "-p",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Whatever changed while no pw-mon was running was never
            # reported, so no cached volume from before can be trusted.
            pipewire.forget_volumes()
            await _read_events(proc, on_change)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Covers a missing pw-mon binary (OSError) as well as a crash
            # mid-stream - either way, retrying is better than silently
            # losing hardware-knob tracking until the next redeploy.
            logger.exception("pw-mon watcher crashed, restarting")
        logger.warning("pw-mon exited, restarting in 2s")
        await asyncio.sleep(2)
