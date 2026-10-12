"""The "Reset audio" button: restart the host's PipeWire stack from the dashboard.

The container can't restart host services, so this only writes
data/reset-request. bragi-audio-reset.path on the host watches that file and
runs host/bragi-audio-reset.sh, which saves a snapshot of the graph to
data/incidents/<stamp>-<reason>/ and then restarts PipeWire, WirePlumber and
bragi-peers. The snapshot gets a `done` file once the restart has finished.

Added after deck-assistant #108: every sender's audio came out of the headset
as garbled bursts until the stack was restarted, and nothing was captured
from before that restart to say why.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import peers

# Snapshot plus restart takes ~15 s on the Pi. Past this, assume the host
# units aren't installed (or failed) rather than leave the button locked.
_STALE_AFTER = timedelta(minutes=2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _request_file():
    return peers.DATA_DIR / 'reset-request'


def _requested_at() -> datetime | None:
    try:
        return datetime.fromisoformat(_request_file().read_text().strip())
    except (OSError, ValueError):
        return None


def _finished_snapshots() -> list[tuple[datetime, str]]:
    root = peers.DATA_DIR / 'incidents'
    if not root.is_dir():
        return []
    found = []
    for path in root.iterdir():
        stamp, _, reason = path.name.partition('-')
        clock, _, reason = reason.partition('-')
        if not (path / 'done').exists():
            continue
        try:
            # The host script names snapshots in UTC (date -u).
            when = datetime.strptime(stamp + clock, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        found.append((when, reason or 'manual'))
    return sorted(found)


def view() -> dict:
    snapshots = _finished_snapshots()
    last = snapshots[-1] if snapshots else None
    requested = _requested_at()
    state = 'idle'
    if requested and not (last and last[0] >= requested.replace(microsecond=0)):
        state = 'stale' if _now() - requested > _STALE_AFTER else 'pending'
    return {
        'state': state,
        'last': {'when': last[0].strftime('%Y-%m-%d %H:%M'), 'reason': last[1]} if last else None,
    }


def request() -> None:
    """Raises RuntimeError if a reset is already under way."""
    if view()['state'] == 'pending':
        raise RuntimeError('A reset is already running')
    peers.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _request_file()
    pending = path.with_suffix('.tmp')
    pending.write_text(_now().replace(microsecond=0).isoformat() + '\n')
    pending.replace(path)
