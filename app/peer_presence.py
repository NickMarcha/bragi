"""Tracks which Roc peers currently have a live WebSocket connection from
their own Bragi Client tray app - real proof that peer machine is up and
network-reachable, unlike PipeWire node presence (a Roc module can exist
locally on both ends independent of whether the other side is actually
there, since Roc runs over connectionless UDP - see client/README.md).

Deliberately its own module, not part of ws.py: that file's throttle/
broadcast machinery for fader control is fragile and explicitly "still
under observation" (see its own docstring) - this has nothing to do with
volume/mute and shouldn't share any state or code path with it.
"""

from __future__ import annotations

connected_peers: set[str] = set()
