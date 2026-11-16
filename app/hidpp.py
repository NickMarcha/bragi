"""Logitech mice and keyboards behind a receiver (Bolt, Unifying), over HID++.

Their extra buttons trigger Bragi actions (actions.py), and their battery
shows in the hardware inventory. Ported from the Deskflow fork's
HidppListener and HidppManager (src/lib/platform/), which did both inside
the KVM server; step 3 of desk-hub-vision.md moves them here.

How it works:
- Each receiver has a HID++ hidraw node (Logitech vendor id, a descriptor
  with long report 0x11). Bragi keeps it open: reads and writes on it don't
  disturb the mouse's normal input, which comes through evdev to the KVM.
- Paired devices sit in slots 1-6. For each one Bragi asks its name, its
  reprogrammable controls (feature 0x1b04) and its battery feature (0x1004,
  or 0x1000 on older devices).
- A control with an action is "diverted": instead of acting as a button,
  the device reports presses as HID++ notifications, which run the action.
  Controls without one are set back to normal explicitly, so a diversion
  left over from before (the fork's) can't swallow them.
- A device switched off and on forgets its diversions. The receiver says
  when a device (re)connects (notification 0x41, once its wireless
  notifications are on, which hid-generic never does), and Bragi probes
  that slot again. Slots that didn't answer (a mouse asleep) are tried
  again every 15 seconds.

Actions are saved per device name and control id in data/mouse_buttons.yaml.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import actions, peers

logger = logging.getLogger("bragi.hidpp")

SETTINGS_FILE = peers.DATA_DIR / "mouse_buttons.yaml"

SHORT, LONG = 0x10, 0x11
LONG_LENGTH = 20
# Bragi's software id in requests (the fork used 0x0B and 0x0C), so replies
# meant for anyone else on the node are never taken for ours.
SWID = 0x0D
ROOT, DEVICE_NAME, BATTERY_STATUS, UNIFIED_BATTERY, REPROG_CONTROLS = 0x0000, 0x0005, 0x1000, 0x1004, 0x1b04
CONNECTION_NOTICE = 0x41
ERROR_V2, ERROR_V1 = 0xFF, 0x8F
ERR_UNKNOWN_DEVICE = 0x09
FLAG_DIVERTABLE, FLAG_VIRTUAL = 0x20, 0x80
DIVERT, DIVERT_VALID = 0x01, 0x02
# Left, right, middle, back, forward: these come through evdev already.
PLAIN_BUTTONS = {0x50, 0x51, 0x52, 0x53, 0x56}
CONTROL_NAMES = {0xC3: "Thumb button", 0xC4: "Wheel mode button", 0xD7: "Scroll wheel left/right",
                 0x5D: "Top button", 0xC0: "Back paddle", 0xFD: "Smart shift"}

RESCAN_SECONDS = 5
REPROBE_SECONDS = 15
BATTERY_SECONDS = 15 * 60
REQUEST_TIMEOUT = 0.5


def control_name(cid: int) -> str:
    return CONTROL_NAMES.get(cid, f"Button 0x{cid:02x}")


def find_nodes(sys_root: str = "/sys") -> list[str]:
    """The /dev/hidrawN of every Logitech HID++ interface."""
    out = []
    base = Path(sys_root) / "class" / "hidraw"
    with contextlib.suppress(OSError):
        for entry in sorted(base.iterdir()):
            try:
                uevent = (entry / "device" / "uevent").read_text()
                descriptor = (entry / "device" / "report_descriptor").read_bytes()
            except OSError:
                continue
            if ":0000046D:" in uevent.upper() and b"\x85\x11" in descriptor:
                out.append(f"/dev/{entry.name}")
    return out


def battery_from(unified: bool, reply: bytes) -> dict | None:
    """Percent and state from a battery reply (0x1004 fn 1 or 0x1000 fn 0)."""
    if len(reply) < 3:
        return None
    if unified:
        state = {0: "discharging", 1: "charging", 2: "charging", 3: "full"}.get(reply[2], "error")
    else:
        state = {0: "discharging", 1: "charging", 2: "charging", 3: "full", 4: "charging"}.get(reply[2], "error")
    return {"percent": reply[0], "state": state, "at": time.time()}


def diverted_cids(frame: bytes) -> set[int]:
    """The controls held down, from a divertedButtonsEvent (up to four)."""
    return {cid for k in range(4) if (cid := (frame[4 + 2 * k] << 8) | frame[5 + 2 * k])}


# --- settings ------------------------------------------------------------------

_settings_lock = threading.Lock()


def _load() -> dict:
    with _settings_lock:
        if not SETTINGS_FILE.exists():
            return {}
        data = yaml.safe_load(SETTINGS_FILE.read_text()) or {}
    return data if isinstance(data, dict) else {}


def action_for(device: str, cid: int) -> dict:
    saved = (_load().get(device) or {}).get(f"0x{cid:02x}")
    return saved if isinstance(saved, dict) and saved.get("type") in actions.KVM_KINDS else {"type": "none"}


def valid_action(action) -> dict:
    if not isinstance(action, dict):
        raise ValueError("an action is an object with a type")
    if action.get("type") == "none":
        return {"type": "none"}
    return actions.valid_kvm(action)


def save_action(device: str, cid: int, action: dict) -> None:
    with _settings_lock:
        data = (yaml.safe_load(SETTINGS_FILE.read_text()) or {}) if SETTINGS_FILE.exists() else {}
        entry = data.get(device) or {}
        if action["type"] == "none":
            entry.pop(f"0x{cid:02x}", None)
        else:
            entry[f"0x{cid:02x}"] = action
        if entry:
            data[device] = entry
        else:
            data.pop(device, None)
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        pending = SETTINGS_FILE.with_suffix(".tmp")
        pending.write_text(yaml.safe_dump(data, sort_keys=False))
        pending.replace(SETTINGS_FILE)


# --- one receiver ------------------------------------------------------------------

@dataclass
class Device:
    index: int
    name: str = ""
    reprog: int = 0              # feature index of 0x1b04, 0 if none
    battery_feature: int = 0
    unified_battery: bool = False
    controls: list[int] = field(default_factory=list)
    diverted: set[int] = field(default_factory=set)
    pressed: set[int] = field(default_factory=set)
    battery: dict | None = None


class Link:
    """One open HID++ node: requests one at a time, and notifications.

    `write` sends a report; every report read from the node goes to feed().
    on_press(link, device, cid) runs for each control pressed down;
    on_connect(link, index) when a paired device (re)connects."""

    def __init__(self, path: str, write: Callable[[bytes], None],
                 on_press: Callable[["Link", Device, int], None],
                 on_connect: Callable[["Link", int], None],
                 wanted: Callable[[str, int], bool]) -> None:
        self.path = path
        self._write = write
        self._on_press = on_press
        self._on_connect = on_connect
        self._wanted = wanted
        self._lock = asyncio.Lock()
        self._waiting: tuple[Callable[[bytes], tuple[bool, bytes | None]], asyncio.Future] | None = None
        self.last_error = 0
        self.devices: dict[int, Device] = {}
        self.unanswered: set[int] = set()

    # --- talking to it ---

    async def _ask(self, message: bytes, match, timeout: float) -> bytes | None:
        async with self._lock:
            future = asyncio.get_running_loop().create_future()
            self._waiting = (match, future)
            self.last_error = 0
            try:
                self._write(message)
                return await asyncio.wait_for(future, timeout)
            except (OSError, asyncio.TimeoutError):
                return None
            finally:
                self._waiting = None

    async def request(self, dev: int, feature: int, function: int, params: bytes = b"",
                      timeout: float = REQUEST_TIMEOUT) -> bytes | None:
        """A HID++ 2.0 call; the reply's parameters, or None (no answer, or
        an error, in last_error)."""
        message = bytes([LONG, dev, feature, (function << 4) | SWID]) + bytes(params)[:LONG_LENGTH - 4]
        message += bytes(LONG_LENGTH - len(message))

        def match(frame: bytes) -> tuple[bool, bytes | None]:
            if len(frame) < 6 or frame[1] != dev:
                return False, None
            if frame[0] == LONG and frame[2] == feature and frame[3] & 0x0F == SWID:
                return True, bytes(frame[4:])
            if frame[2] in (ERROR_V2, ERROR_V1) and frame[3] == feature and frame[4] & 0x0F == SWID:
                self.last_error = frame[5]
                return True, None
            return False, None

        return await self._ask(message, match, timeout)

    async def register(self, sub_id: int, params: bytes) -> bytes | None:
        """A HID++ 1.0 register call to the receiver itself (device 0xFF)."""
        message = bytes([SHORT, 0xFF, sub_id, 0x00]) + bytes(params)[:3].ljust(3, b"\0")

        def match(frame: bytes) -> tuple[bool, bytes | None]:
            if len(frame) >= 7 and frame[1] == 0xFF and frame[2] == sub_id and frame[3] == 0x00:
                return True, bytes(frame[4:7])
            if len(frame) >= 7 and frame[1] == 0xFF and frame[2] == ERROR_V1 and frame[3] == sub_id:
                return True, None
            return False, None

        return await self._ask(message, match, REQUEST_TIMEOUT)

    def feed(self, frame: bytes) -> None:
        """A report read from the node: a reply we wait for, or a notification."""
        waiting = self._waiting
        if waiting is not None:
            matched, value = waiting[0](frame)
            if matched:
                if not waiting[1].done():
                    waiting[1].set_result(value)
                return
        self._notification(frame)

    def _notification(self, frame: bytes) -> None:
        if len(frame) >= 5 and frame[0] == SHORT and frame[2] == CONNECTION_NOTICE:
            index = frame[1]
            if frame[4] & 0x40 == 0:  # link up
                self._on_connect(self, index)
            elif (dev := self.devices.get(index)) is not None:
                dev.pressed.clear()
            return
        dev = self.devices.get(frame[1]) if len(frame) >= 12 and frame[0] == LONG else None
        # divertedButtonsEvent: function 0, software id 0.
        if dev is None or not dev.reprog or frame[2] != dev.reprog or frame[3] != 0x00:
            return
        now = diverted_cids(frame)
        down = now - dev.pressed
        dev.pressed = now
        for cid in sorted(down):
            self._on_press(self, dev, cid)

    # --- what's behind it ---

    async def enable_notifications(self) -> None:
        """The receiver reports (re)connections only once its "wireless
        notifications" bit (register 0x00) is on."""
        flags = await self.register(0x81, b"\0\0\0")
        if flags is None or flags[1] & 0x01:
            return
        await self.register(0x80, bytes([flags[0], flags[1] | 0x01, flags[2]]))

    async def feature_index(self, dev: int, feature: int) -> int:
        reply = await self.request(dev, ROOT, 0, bytes([feature >> 8, feature & 0xFF]))
        return reply[0] if reply else 0

    async def probe(self, index: int) -> None:
        """Learns what's in a slot, sets its controls' diversion and reads its
        battery. A slot that doesn't answer is tried again later."""
        reply = await self.request(index, ROOT, 0, bytes([DEVICE_NAME >> 8, DEVICE_NAME & 0xFF]))
        if reply is None:
            # 0x09 means nothing is paired there; else asleep or reconnecting.
            if self.last_error == ERR_UNKNOWN_DEVICE:
                self.unanswered.discard(index)
            else:
                self.unanswered.add(index)
            return
        self.unanswered.discard(index)
        dev = Device(index)
        if reply[0] and (length := await self.request(index, reply[0], 0)):
            name = b""
            while len(name) < length[0]:
                part = await self.request(index, reply[0], 1, bytes([len(name)]))
                if not part or not part.strip(b"\0"):
                    break
                name += part.rstrip(b"\0")[:length[0] - len(name)]
            dev.name = name.decode(errors="replace")
        dev.reprog = await self.feature_index(index, REPROG_CONTROLS)
        if dev.reprog and (count := await self.request(index, dev.reprog, 0)):
            for i in range(count[0]):
                info = await self.request(index, dev.reprog, 1, bytes([i]))
                if not info or len(info) < 5:
                    continue
                cid, flags = (info[0] << 8) | info[1], info[4]
                if flags & FLAG_DIVERTABLE and not flags & FLAG_VIRTUAL and cid not in PLAIN_BUTTONS:
                    dev.controls.append(cid)
        dev.battery_feature = await self.feature_index(index, UNIFIED_BATTERY)
        dev.unified_battery = bool(dev.battery_feature)
        if not dev.battery_feature:
            dev.battery_feature = await self.feature_index(index, BATTERY_STATUS)
        if not dev.reprog and not dev.battery_feature:
            self.devices.pop(index, None)
            return
        old = self.devices.get(index)
        if old is not None and old.name == dev.name:
            dev.battery = old.battery
        self.devices[index] = dev
        await self.apply_diversion(dev, force=True)
        await self.read_battery(dev)
        logger.info("hidpp: %r on %s slot %d: controls %s, battery %s", dev.name, self.path, index,
                    " ".join(f"0x{c:02x}" for c in dev.controls) or "none",
                    dev.battery and f"{dev.battery['percent']}% {dev.battery['state']}")

    async def apply_diversion(self, dev: Device, force: bool = False) -> None:
        """Diverts the controls with an action and sets the rest back."""
        for cid in dev.controls:
            want = self._wanted(dev.name, cid)
            if not force and want == (cid in dev.diverted):
                continue
            flags = DIVERT | DIVERT_VALID if want else DIVERT_VALID
            reply = await self.request(dev.index, dev.reprog, 3, bytes([cid >> 8, cid & 0xFF, flags, 0, 0]))
            if reply is None:
                continue
            (dev.diverted.add if want else dev.diverted.discard)(cid)

    async def read_battery(self, dev: Device) -> None:
        if not dev.battery_feature:
            return
        reply = await self.request(dev.index, dev.battery_feature, 1 if dev.unified_battery else 0)
        if reply is not None and (battery := battery_from(dev.unified_battery, reply)):
            dev.battery = battery


# --- all receivers ---------------------------------------------------------------

class Manager:
    def __init__(self) -> None:
        self.links: dict[str, Link] = {}
        self._files: dict[str, int] = {}
        self._results: dict[tuple[str, int], dict] = {}
        self._running: set[tuple[str, int]] = set()
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def wanted(self, device: str, cid: int) -> bool:
        return action_for(device, cid)["type"] != "none"

    def on_press(self, link: Link, dev: Device, cid: int) -> None:
        action = action_for(dev.name, cid)
        key = (dev.name, cid)
        if action["type"] == "none" or key in self._running:
            return
        self._running.add(key)

        async def run():
            try:
                ok, text = await actions.run_kvm(action)
            except Exception as exc:  # an action must never take the reader down
                logger.exception("hidpp: %s on %r failed", control_name(cid), dev.name)
                ok, text = False, str(exc) or type(exc).__name__
            finally:
                self._running.discard(key)
            if not ok:
                logger.warning("hidpp: %s on %r: %s", control_name(cid), dev.name, text)
            self._results[key] = {"ok": ok, "text": text, "at": time.time()}

        self._spawn(run())

    def on_connect(self, link: Link, index: int) -> None:
        async def later():
            await asyncio.sleep(0.5)  # it answers only once the link has settled
            await link.probe(index)

        self._spawn(later())

    # --- nodes ---

    def _open(self, path: str) -> None:
        dev_root = os.environ.get("BRAGI_HOST_DEV", "/dev")
        try:
            fd = os.open(path.replace("/dev", dev_root, 1), os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            logger.warning("hidpp: can't open %s: %s", path, exc)
            return
        link = Link(path, lambda data: os.write(fd, data), self.on_press, self.on_connect, self.wanted)

        def readable():
            while True:
                try:
                    frame = os.read(fd, 64)
                except BlockingIOError:
                    return
                except OSError:  # unplugged: the next rescan closes it
                    with contextlib.suppress(Exception):
                        asyncio.get_running_loop().remove_reader(fd)
                    return
                if not frame:
                    return
                link.feed(frame)

        asyncio.get_running_loop().add_reader(fd, readable)
        self.links[path] = link
        self._files[path] = fd

        async def start():
            await link.enable_notifications()
            for index in range(1, 7):
                await link.probe(index)

        self._spawn(start())

    def _close(self, path: str) -> None:
        self.links.pop(path, None)
        fd = self._files.pop(path, None)
        if fd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(fd)
            with contextlib.suppress(OSError):
                os.close(fd)

    async def run(self) -> None:
        last_reprobe = last_battery = time.monotonic()
        try:
            while True:
                try:
                    present = set(await asyncio.to_thread(find_nodes))
                    for path in set(self.links) - present:
                        self._close(path)
                    for path in sorted(present - set(self.links)):
                        self._open(path)
                    now = time.monotonic()
                    if now - last_reprobe >= REPROBE_SECONDS:
                        last_reprobe = now
                        for link in list(self.links.values()):
                            for index in sorted(link.unanswered):
                                await link.probe(index)
                            for dev in list(link.devices.values()):
                                await link.apply_diversion(dev)
                    if now - last_battery >= BATTERY_SECONDS:
                        last_battery = now
                        for link in list(self.links.values()):
                            for dev in list(link.devices.values()):
                                await link.read_battery(dev)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("hidpp rescan failed")
                await asyncio.sleep(RESCAN_SECONDS)
        finally:
            for path in list(self.links):
                self._close(path)

    # --- the dashboard ---

    def devices(self) -> list[dict]:
        out = []
        for link in self.links.values():
            for dev in sorted(link.devices.values(), key=lambda d: d.index):
                out.append({"name": dev.name, "node": link.path, "slot": dev.index, "battery": dev.battery,
                            "controls": [{"cid": cid, "id": f"0x{cid:02x}", "label": control_name(cid),
                                          "action": action_for(dev.name, cid),
                                          "result": self._results.get((dev.name, cid))}
                                         for cid in dev.controls]})
        return out

    async def set_action(self, device: str, cid: int, action) -> dict:
        action = valid_action(action)
        await asyncio.to_thread(save_action, device, cid, action)
        for link in self.links.values():
            for dev in list(link.devices.values()):
                if dev.name == device:
                    await link.apply_diversion(dev)
        return action


manager: Manager | None = None


async def supervise() -> None:
    global manager
    manager = Manager()
    await manager.run()
