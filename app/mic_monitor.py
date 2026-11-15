"""Software mic monitoring: a pw-loopback on the host from a USB headset's
own mic to its own speaker, so you hear yourself.

The HyperX Cloud III S Wireless has a hardware monitor (headset_hid.py's
OP_MIC_MONITOR_WRITE). It switches on, but on the Pi's headset it only plays
noise while the USB mic itself is fine. The loopback here was tried by hand
on the Pi first, at 256/48000 on both sides. That pulls the whole graph's
quantum from 1024 down to 256 while it runs. pw-top showed no new errors on
any Roc or VBAN node over 10 seconds and there was no crackling. At 1024 the
delay was easy to hear.

One pw-loopback process per headset, a child of Bragi, rather than a module
in peers.conf: it has to come and go with a switch, and restarting
bragi-peers.service for that would drop every peer's audio. A child process
also cleans up after itself: when Bragi's container stops, the process goes
with it and PipeWire removes its nodes. Anything left over from a Bragi run
outside a container is swept on startup (sweep_strays).

The loopback is stopped whenever either of the headset's nodes is missing
(unplugged, disabled) and started again when both are back, so a replug
needs nothing from WirePlumber. node.dont-fallback covers the couple of
seconds before supervise() notices: without it WirePlumber would move the
playback stream to the default sink, which can be a peer's.

Volume is the playback stream's own volume, so it changes neither the mic
level the peers get (their loopbacks read the mic directly) nor the
headset's playback volume.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from dataclasses import dataclass

import yaml

from . import headsets, peers, pipewire

logger = logging.getLogger("bragi.mic_monitor")

SETTINGS_FILE = peers.DATA_DIR / "mic_monitor.yaml"
LATENCY = "256/48000"
NODE_PREFIX = "bragi-mic-monitor"
DEFAULT_VOLUME = 1.0
MAX_VOLUME = 1.5  # pipewire.set_volume's ceiling, same as the faders

_RECONCILE_SECONDS = 2
# How soon to look again after starting a loopback, for its playback node
# to show up so the saved volume can go on it, and for how long.
_STARTING_SECONDS = 0.25
_STARTING_FOR_SECONDS = 5
_settings_lock = threading.Lock()


def _load() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    return yaml.safe_load(SETTINGS_FILE.read_text()) or {}


def settings_for(card_id: str) -> dict:
    with _settings_lock:
        saved = _load().get(card_id) or {}
    return {"enabled": bool(saved.get("enabled", False)),
            "volume": float(saved.get("volume", DEFAULT_VOLUME))}


def _save(card_id: str, **changes) -> dict:
    with _settings_lock:
        data = _load()
        entry = {**{"enabled": False, "volume": DEFAULT_VOLUME}, **(data.get(card_id) or {}), **changes}
        data[card_id] = entry
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        pending = SETTINGS_FILE.with_suffix(".tmp")
        pending.write_text(yaml.safe_dump(data, sort_keys=False))
        pending.replace(SETTINGS_FILE)
    return entry


def valid_volume(value) -> float:
    if type(value) not in (int, float) or not 0 <= value <= MAX_VOLUME:
        raise ValueError(f"monitor volume is a number from 0 to {MAX_VOLUME:g}")
    return round(float(value), 2)


def node_names(card_id: str) -> tuple[str, str]:
    return f"{NODE_PREFIX}.{card_id}.capture", f"{NODE_PREFIX}.{card_id}.playback"


def command(card_id: str, source: str, sink: str) -> list[str]:
    capture_name, playback_name = node_names(card_id)
    # No node.linger, unlike peers.py's loopbacks: it would keep these
    # nodes after the process exits, which is the stray loopback this
    # module is meant never to leave.
    common = f"node.latency={LATENCY} node.dont-fallback=true state.restore-target=false"
    return [
        "pw-loopback",
        "--name", f"{NODE_PREFIX}.{card_id}",
        f"--capture-props=target.object={source} node.name={capture_name} {common}",
        f"--playback-props=target.object={sink} node.name={playback_name} "
        f'node.description="Bragi mic monitor" {common}',
    ]


async def _spawn(args: list[str]):
    """The seam tests replace: starts one pw-loopback."""
    return await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )


@dataclass
class _Loopback:
    proc: object
    # The headset's source and sink, by name and id: a replug brings the
    # same names back with new ids, and that restarts the loopback too.
    targets: tuple
    started_at: float = 0.0
    node_id: int | None = None  # the playback stream, once it's in the graph
    applied_volume: float | None = None


class Runner:
    """Owns the running loopbacks. Only touched from the event loop."""

    def __init__(self) -> None:
        self.running: dict[str, _Loopback] = {}
        self.wake = asyncio.Event()
        # Keeps a fast slider's steps in the order they arrived: each
        # WebSocket action runs as its own task.
        self.lock = asyncio.Lock()

    def starting(self) -> bool:
        now = asyncio.get_running_loop().time()
        return any(lb.applied_volume is None and now - lb.started_at < _STARTING_FOR_SECONDS
                   for lb in self.running.values())

    async def reconcile(self) -> None:
        # Under the lock, so a slider step can't land between reading the
        # saved volume and applying it, and get undone.
        async with self.lock:
            await self._reconcile()

    async def _reconcile(self) -> None:
        with _settings_lock:
            saved = _load()
        wanted = {key: entry for key, entry in saved.items() if entry.get("enabled")}
        if not wanted and not self.running:
            return  # nothing on, nothing to look at: no pw-dump
        graph = await asyncio.to_thread(pipewire.dump)
        targets = {
            hs.key: (hs.capture_node_name, hs.capture_node_id, hs.playback_node_name, hs.playback_node_id)
            for hs in headsets.list_headsets(graph)
            if hs.key in wanted and hs.capture_node_name and hs.playback_node_name
        }

        for key, lb in list(self.running.items()):
            if getattr(lb.proc, "returncode", None) is not None:
                logger.warning("mic monitor for %s exited (%s), starting it again", key, lb.proc.returncode)
                del self.running[key]
            elif targets.get(key) != lb.targets:
                await self._stop(key)

        for key, target in targets.items():
            if key in self.running:
                continue
            try:
                proc = await _spawn(command(key, target[0], target[2]))
            except OSError:
                logger.exception("could not start the mic monitor for %s", key)
                continue
            logger.info("mic monitor on for %s", key)
            self.running[key] = _Loopback(proc, target, asyncio.get_running_loop().time())

        playback_ids = {node.name: node.id for node in graph.nodes}
        for key, lb in self.running.items():
            if lb.node_id is None:
                lb.node_id = playback_ids.get(node_names(key)[1])
            volume = float(wanted[key].get("volume", DEFAULT_VOLUME))
            if lb.node_id is not None and lb.applied_volume != volume:
                await self._apply_volume(key, lb, volume)

    async def _apply_volume(self, key: str, lb: _Loopback, volume: float) -> None:
        try:
            await asyncio.to_thread(pipewire.set_volume, lb.node_id, volume)
        except pipewire.PipewireError:
            # Gone with the headset, or the process restarted: look again.
            logger.info("mic monitor volume for %s didn't apply, looking for its node again", key)
            lb.node_id = None
            self.wake.set()
            return
        lb.applied_volume = volume

    async def set_volume(self, key: str, volume: float) -> None:
        """Straight onto the running stream when it can, so dragging the
        slider costs one wpctl per step, not a pw-dump."""
        lb = self.running.get(key)
        if lb is not None and lb.node_id is not None:
            await self._apply_volume(key, lb, volume)
        else:
            self.wake.set()

    async def _stop(self, key: str) -> None:
        lb = self.running.pop(key)
        logger.info("mic monitor off for %s", key)
        await _terminate(lb.proc)

    async def stop_all(self) -> None:
        for key in list(self.running):
            await self._stop(key)


async def _terminate(proc) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


_runner: Runner | None = None


def runner() -> Runner:
    global _runner
    if _runner is None:
        _runner = Runner()
    return _runner


async def set_enabled(card_id: str, enabled: bool) -> dict:
    if not isinstance(enabled, bool):
        raise ValueError("mic monitoring is true or false")
    async with runner().lock:
        entry = await asyncio.to_thread(_save, card_id, enabled=enabled)
    runner().wake.set()
    return entry


async def set_volume(card_id: str, volume) -> dict:
    volume = valid_volume(volume)
    async with runner().lock:
        entry = await asyncio.to_thread(_save, card_id, volume=volume)
        if entry["enabled"]:
            await runner().set_volume(card_id, volume)
    return entry


def sweep_strays() -> list[int]:
    """Disconnects every PipeWire client that owns a node named like ours.
    Run before supervise() starts any, so whatever it finds was left by an
    earlier Bragi. Returns the client ids it destroyed."""
    objects = json.loads(pipewire._run(["pw-dump"]))
    clients = sorted({
        int(props["client.id"])
        for obj in objects if obj.get("type") == "PipeWire:Interface:Node"
        for props in [(obj.get("info") or {}).get("props") or {}]
        if str(props.get("node.name", "")).startswith(f"{NODE_PREFIX}.") and props.get("client.id") is not None
    })
    for client_id in clients:
        try:
            pipewire._run(["pw-cli", "destroy", str(client_id)])
            logger.warning("removed a mic monitor loopback left over from an earlier run (client %s)", client_id)
        except pipewire.PipewireError:
            logger.exception("could not remove leftover mic monitor client %s", client_id)
    return clients


async def supervise() -> None:
    """Runs forever as a lifespan task, like microphone.supervise(). Turns
    the saved switches into running loopbacks, every couple of seconds or
    as soon as a switch changes."""
    r = runner()
    try:
        await asyncio.to_thread(sweep_strays)
    except Exception:
        logger.exception("could not check for leftover mic monitor loopbacks")
    try:
        while True:
            r.wake.clear()
            try:
                await r.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("mic monitor reconcile failed")
            timeout = _STARTING_SECONDS if r.starting() else _RECONCILE_SECONDS
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(r.wake.wait(), timeout=timeout)
    finally:
        await r.stop_all()
