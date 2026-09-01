"""Shared waiting helper.

ws.py applies slider actions on a background per-control worker rather than
inline (see its module docstring for why), so a test that asserts on the
result has to wait for that worker instead of assuming the await returned
with the work done.
"""

from __future__ import annotations

import asyncio

from app import ws


async def settle(timeout: float = 3.0) -> None:
    """Returns once every throttle worker has drained its pending value."""
    deadline = asyncio.get_event_loop().time() + timeout
    while ws._pending_value or ws._worker_running:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("throttle workers did not settle")
        await asyncio.sleep(0.005)
    await asyncio.sleep(0)
