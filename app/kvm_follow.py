"""Switches the KVM layout when the setup changes (step 4 of
docs/kvm-layout.md).

monitors.report() calls changed() whenever a tray app's monitors differ
from before. After SETTLE seconds without another change, follow_setup()
puts the layout saved for the setup connected now in use, if there is
one. The wait keeps a TV switching inputs, or a monitor waking up, from
flipping the layout back and forth.
"""

from __future__ import annotations

import asyncio
import logging

from . import kvm_config

logger = logging.getLogger("bragi.kvm_follow")

SETTLE = 15.0

_loop: asyncio.AbstractEventLoop | None = None
_broadcast = None
_timer: asyncio.TimerHandle | None = None


def start(loop: asyncio.AbstractEventLoop, broadcast) -> None:
    global _loop, _broadcast
    _loop, _broadcast = loop, broadcast


def changed() -> None:
    """Monitors changed somewhere: (re)start the wait. Safe from any thread."""
    if _loop is not None:
        _loop.call_soon_threadsafe(_restart_timer)


def _restart_timer() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
    _timer = _loop.call_later(SETTLE, lambda: asyncio.ensure_future(_follow()))


async def _follow() -> None:
    try:
        note = await asyncio.to_thread(kvm_config.follow_setup)
    except kvm_config.ConfigError as exc:
        logger.warning("layout not switched: %s", exc)
        return
    except Exception:
        logger.exception("layout switch failed")
        return
    if note:
        logger.info("%s", note)
        if _broadcast is not None:
            _broadcast({"type": "kvm_layout", "note": note})
