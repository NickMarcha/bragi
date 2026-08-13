"""Translates each headset's physical volume knob into an actual PipeWire
volume change.

These headsets' knobs don't touch ALSA's hardware mixer at all - confirmed
live by reading raw input events while turning one: it sends standard
KEY_VOLUMEUP/KEY_VOLUMEDOWN media-key codes through a separate USB HID
"Consumer Control" interface, once per detent. On a normal desktop, the
running session's media-key handler catches that and calls into PipeWire -
that's the whole reason the knob "just works" there. sagepi is headless
(Raspberry Pi OS Lite, no desktop session), so nothing is listening for
those keys at all; this module is that listener, purpose-built for exactly
this one job, run continuously since Bragi is the only thing on this box
that would ever do it.

Each headset's Consumer Control device is found dynamically (never a
hardcoded /dev/input/eventN, which can renumber across reconnects) by
matching /proc/bus/input/devices' Uniq field - the same USB serial that's
already embedded in the headset's card_id (see headsets.py) - against a
device named "Consumer Control".
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import headsets as headsets_module
from . import pipewire

logger = logging.getLogger("bragi.knob")

_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
_EV_KEY = 1
_KEY_VOLUMEUP = 115
_KEY_VOLUMEDOWN = 114
_VOLUME_STEP = 0.02
_RESCAN_SECONDS = 5

OnVolumeChanged = Callable[[str], Awaitable[None]]


def _consumer_control_devices() -> dict[str, str]:
    """{uniq (USB serial): '/dev/input/eventN'} for every currently-present
    'Consumer Control' HID device."""
    devices: dict[str, str] = {}
    try:
        text = Path("/proc/bus/input/devices").read_text()
    except OSError:
        return devices
    for block in text.split("\n\n"):
        if "Consumer Control" not in block:
            continue
        uniq_match = re.search(r"^U: Uniq=(\S*)", block, re.MULTILINE)
        handler_match = re.search(r"^H: Handlers=.*?\b(event\d+)\b", block, re.MULTILINE)
        if not handler_match:
            continue
        uniq = uniq_match.group(1) if uniq_match else ""
        if uniq:
            devices[uniq] = f"/dev/input/{handler_match.group(1)}"
    return devices


def _find_device_for_headset(card_id: str, devices: dict[str, str]) -> str | None:
    for uniq, path in devices.items():
        if uniq in card_id:
            return path
    return None


async def watch(on_volume_changed: OnVolumeChanged) -> None:
    """Runs forever. on_volume_changed(headset_key) is awaited after a knob
    press actually changes that headset's PipeWire volume, so the caller
    can broadcast it (see main.py's lifespan wiring)."""
    loop = asyncio.get_event_loop()
    open_files: dict[str, tuple[int, object]] = {}  # headset key -> (fd, file)

    def _on_readable(key: str, node_id: int, f) -> None:
        try:
            data = f.read(_EVENT_SIZE)
        except OSError:
            return
        if not data or len(data) < _EVENT_SIZE:
            return
        _sec, _usec, etype, code, value = struct.unpack(_EVENT_FMT, data)
        if etype != _EV_KEY or value == 0:
            return  # ignore key-up and non-key events (EV_MSC scancode echo, etc.)
        if code == _KEY_VOLUMEUP:
            step = _VOLUME_STEP
        elif code == _KEY_VOLUMEDOWN:
            step = -_VOLUME_STEP
        else:
            return
        asyncio.create_task(_step_and_notify(node_id, step, key, on_volume_changed))

    try:
        while True:
            try:
                devices = await asyncio.to_thread(_consumer_control_devices)
                nodes = await asyncio.to_thread(pipewire.list_nodes)
                current = await asyncio.to_thread(headsets_module.list_headsets, nodes)
                current_keys = {hs.key for hs in current}

                for key in list(open_files):
                    if key not in current_keys:
                        fd, f = open_files.pop(key)
                        loop.remove_reader(fd)
                        f.close()
                        logger.info("knob watcher: %s unplugged, closed listener", key)

                for hs in current:
                    if hs.key in open_files or hs.playback_node_id is None:
                        continue
                    path = _find_device_for_headset(hs.key, devices)
                    if path is None:
                        continue
                    try:
                        f = open(path, "rb", buffering=0)
                    except OSError:
                        logger.warning("knob watcher: could not open %s for %s", path, hs.key)
                        continue
                    fd = f.fileno()
                    loop.add_reader(fd, _on_readable, hs.key, hs.playback_node_id, f)
                    open_files[hs.key] = (fd, f)
                    logger.info("knob watcher: listening on %s for %s", path, hs.key)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("knob watcher rescan failed, retrying in %ss", _RESCAN_SECONDS)

            await asyncio.sleep(_RESCAN_SECONDS)
    finally:
        for fd, f in open_files.values():
            loop.remove_reader(fd)
            f.close()


async def _step_and_notify(node_id: int, step: float, key: str, on_volume_changed: OnVolumeChanged) -> None:
    def _apply() -> bool:
        volume, _muted = pipewire.get_volume_mute(node_id)
        if volume is None:
            return False
        pipewire.set_volume(node_id, volume + step)
        return True

    if await asyncio.to_thread(_apply):
        await on_volume_changed(key)
