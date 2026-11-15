"""Game controllers paired with the Bragi host, played on one computer.

A controller connects to the host (over Bluetooth on the Pi: forwarding a USB
dongle with USB/IP flooded the shared USB controller and stalled the KVM
mice). This module reads it, turns whatever layout the kernel driver gives
into one Xbox layout, and sends the state to the chosen computer's Bragi
Client over its /ws/peer socket. The client there creates a virtual Xbox
360 controller (client/src/Bragi.Client/Gamepad/).

Which computer gets the controllers is saved in data/gamepad.yaml as
`target`, set from that computer's tray ("Use controller here").

The wire state is xpad's convention, so a Linux client can hand it to uinput
unchanged: sticks -32768..32767 with Y pointing down, triggers 0..255, d-pad
-1/0/1, and the pressed buttons by name.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import peers

logger = logging.getLogger("bragi.gamepad")

SETTINGS_FILE = peers.DATA_DIR / "gamepad.yaml"

_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
_RESCAN_SECONDS = 2

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0, 1, 2, 3, 4, 5
ABS_GAS, ABS_BRAKE, ABS_HAT0X, ABS_HAT0Y = 9, 10, 16, 17

# Same codes under xpad and under hid-generic for the 8BitDo Ultimate 3-mode
# (checked button by button). Its extra buttons (306, 309, 319: share and
# the back paddles) have no Xbox 360 equivalent and are left out.
BUTTONS = {304: "a", 305: "b", 307: "x", 308: "y", 310: "lb", 311: "rb",
           314: "back", 315: "start", 316: "guide", 317: "ls", 318: "rs"}


@dataclass(frozen=True)
class Layout:
    """Which kernel axis carries each Xbox axis."""
    lx: int
    ly: int
    rx: int
    ry: int
    lt: int
    rt: int


# xpad (USB Xbox controllers and their dongles).
XPAD = Layout(ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_Z, ABS_RZ)
# hid-generic, e.g. the 8BitDo over Bluetooth: right stick on Z/RZ, triggers on BRAKE/GAS.
HID_GAMEPAD = Layout(ABS_X, ABS_Y, ABS_Z, ABS_RZ, ABS_BRAKE, ABS_GAS)


def layout_for(abs_codes: set[int]) -> Layout:
    return XPAD if ABS_RX in abs_codes else HID_GAMEPAD


@dataclass(frozen=True)
class Controller:
    id: str
    name: str
    event: str  # "event25"


def list_controllers(text: str) -> list[Controller]:
    """Every joystick in /proc/bus/input/devices: the kernel gives game
    controllers a js handler, keyboards and mice never get one."""
    found = []
    for block in text.split("\n\n"):
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.MULTILINE)
        if not handlers or not re.search(r"\bjs\d+\b", handlers.group(1)):
            continue
        event = re.search(r"\b(event\d+)\b", handlers.group(1))
        if not event:
            continue
        name = re.search(r'^N: Name="(.*)"$', block, re.MULTILINE)
        uniq = re.search(r"^U: Uniq=(\S+)", block, re.MULTILINE)
        label = name.group(1) if name else event.group(1)
        found.append(Controller(id=(uniq.group(1).lower() if uniq else label), name=label, event=event.group(1)))
    return found


def _stick(value: int, low: int, high: int) -> int:
    """-32768..32767 with the resting position at 0. The centre of an even
    range like 0..255 is rounded up (128, where these controllers rest), and
    each side is scaled on its own so both ends still reach full deflection."""
    if high <= low:
        return 0
    centre = (low + high + 1) // 2
    if value >= centre:
        return min(32767, round((value - centre) / max(1, high - centre) * 32767))
    return max(-32768, round((value - centre) / max(1, centre - low) * 32768))


def _trigger(value: int, low: int, high: int) -> int:
    if high <= low:
        return 0
    return max(0, min(255, round((value - low) / (high - low) * 255)))


@dataclass
class Normaliser:
    """Folds one controller's evdev events into the Xbox wire state."""
    layout: Layout
    ranges: dict[int, tuple[int, int]]
    buttons: set[str] = field(default_factory=set)
    axes: dict[str, int] = field(default_factory=lambda: {
        "lx": 0, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "dx": 0, "dy": 0})
    changed: bool = False

    def feed(self, etype: int, code: int, value: int) -> bool:
        """Applies one event. True on a SYN_REPORT that follows a change."""
        if etype == EV_KEY and code in BUTTONS:
            name = BUTTONS[code]
            if (value != 0) != (name in self.buttons):
                (self.buttons.add if value else self.buttons.discard)(name)
                self.changed = True
        elif etype == EV_ABS:
            self._axis(code, value)
        elif etype == EV_SYN and code == 0 and self.changed:
            self.changed = False
            return True
        return False

    def _axis(self, code: int, value: int) -> None:
        layout = self.layout
        low, high = self.ranges.get(code, (0, 255))
        if code in (ABS_HAT0X, ABS_HAT0Y):
            key, new = ("dx" if code == ABS_HAT0X else "dy"), max(-1, min(1, value))
        elif code in (layout.lt, layout.rt):
            key, new = ("lt" if code == layout.lt else "rt"), _trigger(value, low, high)
        elif code in (layout.lx, layout.ly, layout.rx, layout.ry):
            key = {layout.lx: "lx", layout.ly: "ly", layout.rx: "rx", layout.ry: "ry"}[code]
            new = _stick(value, low, high)
        else:
            return
        if self.axes[key] != new:
            self.axes[key] = new
            self.changed = True

    def state(self) -> dict:
        return {"buttons": sorted(self.buttons), **self.axes}


# --- target -----------------------------------------------------------------

_settings_lock = threading.Lock()


def target() -> str | None:
    with _settings_lock:
        if not SETTINGS_FILE.exists():
            return None
        value = (yaml.safe_load(SETTINGS_FILE.read_text()) or {}).get("target")
        return value if isinstance(value, str) and value else None


def _save_target(name: str | None) -> None:
    with _settings_lock:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        pending = SETTINGS_FILE.with_suffix(".tmp")
        pending.write_text(yaml.safe_dump({"target": name}, sort_keys=False))
        pending.replace(SETTINGS_FILE)


# --- live controllers and sending ---------------------------------------------

def _ioc_read(nr: int, size: int) -> int:
    return (2 << 30) | (size << 16) | (ord("E") << 8) | nr


def _abs_codes(fd: int) -> set[int]:
    import fcntl  # Linux only; imported here so the tests run anywhere
    try:
        bits = fcntl.ioctl(fd, _ioc_read(0x20 + EV_ABS, 8), bytes(8))
    except OSError:
        return set()
    mask = int.from_bytes(bits, "little")
    return {code for code in range(64) if mask >> code & 1}


def _abs_range(fd: int, code: int) -> tuple[int, int] | None:
    import fcntl
    try:
        info = fcntl.ioctl(fd, _ioc_read(0x40 + code, 24), bytes(24))
    except OSError:
        return None
    _value, low, high, *_ = struct.unpack("6i", info)
    return low, high


def _device_path(event: str) -> str:
    # Controllers come and go (Bluetooth), and `devices:` in
    # docker-compose.yml only copies the nodes present at start. The host's
    # /dev is mounted read-only at BRAGI_HOST_DEV, and a cgroup rule allows
    # input devices (major 13).
    return f"{os.environ.get('BRAGI_HOST_DEV', '/dev')}/input/{event}"


# --- HID passthrough ----------------------------------------------------------
# A computer that can create HID devices (Linux, /dev/uhid) gets the
# controller itself: its report descriptor and ids once, then its raw input
# reports. The kernel there binds the same driver as here, so extra buttons,
# motion sensors and anything else the controller has arrive as they are.
# Output (rumble, LEDs) and the driver's report requests come back and are
# carried out on the real controller through its hidraw node.

# uhid's report types (linux/uhid.h).
UHID_FEATURE_REPORT, UHID_OUTPUT_REPORT, UHID_INPUT_REPORT = 0, 1, 2
_HID_MAX_REPORT = 4096


def hid_info(event: str, sys_root: str = "/sys") -> dict | None:
    """What a computer needs to recreate this controller, from sysfs: name,
    ids, report descriptor and its hidraw node. None if it has no HID device
    behind it."""
    base = Path(sys_root) / "class" / "input" / event / "device"
    try:
        hid_dir = (base / "device").resolve()
        descriptor = (hid_dir / "report_descriptor").read_bytes()
        uevent = dict(line.split("=", 1) for line in (hid_dir / "uevent").read_text().splitlines() if "=" in line)
        hidraw = sorted(p.name for p in (hid_dir / "hidraw").iterdir())
        version = int((base / "id" / "version").read_text().strip(), 16)
    except (OSError, ValueError):
        return None
    try:
        bus, vendor, product = (int(part, 16) for part in uevent["HID_ID"].split(":"))
    except (KeyError, ValueError):
        return None
    if not hidraw or not descriptor:
        return None
    return {"name": uevent.get("HID_NAME", ""), "uniq": uevent.get("HID_UNIQ", ""), "bus": bus,
            "vendor": vendor, "product": product, "version": version, "country": 0,
            "descriptor": base64.b64encode(descriptor).decode(), "hidraw": hidraw[0]}


def _hidioc(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


def get_report(fd: int, rnum: int, rtype: int) -> bytes:
    """A feature, input or output report read from the real controller."""
    import fcntl
    nr = {UHID_FEATURE_REPORT: 0x07, UHID_INPUT_REPORT: 0x0A, UHID_OUTPUT_REPORT: 0x0C}[rtype]
    buf = bytearray(_HID_MAX_REPORT)
    buf[0] = rnum
    length = fcntl.ioctl(fd, _hidioc(nr, len(buf)), buf, True)
    return bytes(buf[:length])


def set_report(fd: int, rtype: int, data: bytes) -> None:
    """A feature, input or output report written to the real controller. The
    first byte is the report number, as uhid hands it over."""
    import fcntl
    nr = {UHID_FEATURE_REPORT: 0x06, UHID_INPUT_REPORT: 0x09, UHID_OUTPUT_REPORT: 0x0B}[rtype]
    fcntl.ioctl(fd, _hidioc(nr, len(data)), bytearray(data), True)


@dataclass
class _Open:
    controller: Controller
    normaliser: Normaliser
    event_file: object
    hid: dict | None = None      # hid_info() without the hidraw name
    hid_file: object | None = None


class Hub:
    """The open controllers, the target, and what goes to which client."""

    def __init__(self, send, broadcast) -> None:
        # send(peer, message, droppable) -> None; broadcast(message) -> None
        self._send = send
        self._broadcast = broadcast
        self._open: dict[str, _Open] = {}
        self._target = target()
        # What each connected tray app can play: "hid" and/or "xbox".
        self._modes: dict[str, set[str]] = {}

    def status(self) -> dict:
        return {"type": "gamepads", "target": self._target,
                "controllers": [{"id": o.controller.id, "name": o.controller.name} for o in self._open.values()]}

    def _passthrough(self, peer: str, entry: _Open) -> bool:
        return "hid" in self._modes.get(peer, set()) and entry.hid is not None and entry.hid_file is not None

    def _attach(self, peer: str, entry: _Open) -> None:
        if self._passthrough(peer, entry):
            self._send(peer, {"type": "hid_create", "id": entry.controller.id, **entry.hid}, False)
        else:
            self._send(peer, {"type": "gamepad", "id": entry.controller.id, **entry.normaliser.state()}, False)

    def hello(self, peer: str, modes: list) -> None:
        """A tray app says what it can play. Its current controllers are made
        again in the best mode it has."""
        self._modes[peer] = {m for m in modes if m in ("hid", "xbox")}
        if peer == self._target:
            for entry in self._open.values():
                self._send(peer, {"type": "gamepad_detach", "id": entry.controller.id}, False)
                self._attach(peer, entry)

    def forget(self, peer: str) -> None:
        self._modes.pop(peer, None)

    def set_target(self, name: str | None) -> None:
        if name == self._target:
            return
        old, self._target = self._target, name
        _save_target(name)
        for entry in self._open.values():
            if old:
                self._send(old, {"type": "gamepad_detach", "id": entry.controller.id}, False)
            if name:
                self._attach(name, entry)
        self._broadcast(self.status())
        logger.info("gamepad: target %s -> %s", old, name)

    def release(self, name: str) -> None:
        if self._target == name:
            self.set_target(None)

    def opened(self, entry: _Open) -> None:
        self._open[entry.controller.id] = entry
        if self._target:
            self._attach(self._target, entry)
        self._broadcast(self.status())

    def closed(self, controller_id: str) -> _Open | None:
        entry = self._open.pop(controller_id, None)
        if entry is None:
            return None
        if self._target:
            self._send(self._target, {"type": "gamepad_detach", "id": controller_id}, False)
        self._broadcast(self.status())
        return entry

    def input(self, controller_id: str) -> None:
        """An evdev change report: goes to a target playing the Xbox layout."""
        entry = self._open.get(controller_id)
        if entry and self._target and not self._passthrough(self._target, entry):
            # Droppable: a newer state replaces it if the socket falls behind.
            self._send(self._target, {"type": "gamepad", "id": controller_id, **entry.normaliser.state()}, True)

    def hid_input(self, controller_id: str, report: bytes) -> None:
        """A raw input report: goes to a target passing the controller through."""
        entry = self._open.get(controller_id)
        if entry and self._target and self._passthrough(self._target, entry):
            self._send(self._target, {"type": "hid_input", "id": controller_id,
                                      "data": base64.b64encode(report).decode()}, True)

    def hid_request(self, peer: str, message: dict) -> None:
        """Output and report requests from the target's virtual controller,
        carried out on the real one."""
        entry = self._open.get(message.get("id")) if isinstance(message.get("id"), str) else None
        if peer != self._target or entry is None or entry.hid_file is None:
            return
        fd = entry.hid_file.fileno()
        kind = message.get("type")
        try:
            data = base64.b64decode(message.get("data") or "")
            rtype = int(message.get("rtype", UHID_OUTPUT_REPORT))
        except (ValueError, TypeError):
            return
        if kind == "hid_output":
            try:
                os.write(fd, data)
            except OSError as exc:
                logger.debug("gamepad: output to %s failed: %s", entry.controller.id, exc)
            return
        if kind not in ("hid_get_report", "hid_set_report"):
            return
        reply = {"type": "hid_report_reply", "id": entry.controller.id, "kind": kind,
                 "req": message.get("req"), "err": 0, "data": ""}
        try:
            if kind == "hid_get_report":
                reply["data"] = base64.b64encode(get_report(fd, int(message.get("rnum", 0)), rtype)).decode()
            else:
                set_report(fd, rtype, data)
        except (OSError, KeyError, ValueError) as exc:
            reply["err"] = getattr(exc, "errno", None) or 5  # EIO
        self._send(peer, reply, False)

    def open_ids(self) -> set[str]:
        return set(self._open)


hub: Hub | None = None


def _close(loop, entry: _Open | None) -> None:
    if entry is None:
        return
    for handle in (entry.event_file, entry.hid_file):
        if handle is not None:
            with contextlib.suppress(Exception):
                loop.remove_reader(handle.fileno())
            handle.close()


async def supervise(send, broadcast) -> None:
    """Runs forever: opens controllers as they appear, sends their input to
    the target, and closes them when they go."""
    global hub
    hub = Hub(send, broadcast)
    loop = asyncio.get_running_loop()

    def gone(controller: Controller) -> None:
        entry = hub.closed(controller.id)
        if entry is not None:
            _close(loop, entry)
            logger.info("gamepad: %s (%s) went away", controller.name, controller.id)

    def on_event(controller: Controller, normaliser: Normaliser, f) -> None:
        try:
            data = f.read(_EVENT_SIZE * 64)
        except BlockingIOError:
            return
        except OSError:  # ENODEV: the controller disconnected
            data = b""
        if data is None:  # non-blocking read with nothing waiting
            return
        if not data:
            gone(controller)
            return
        report = False
        for offset in range(0, len(data) - _EVENT_SIZE + 1, _EVENT_SIZE):
            _s, _u, etype, code, value = struct.unpack_from(_EVENT_FMT, data, offset)
            report = normaliser.feed(etype, code, value) or report
        if report:
            hub.input(controller.id)

    def on_report(controller: Controller, f) -> None:
        # hidraw hands over one whole report per read.
        try:
            data = f.read(_HID_MAX_REPORT)
        except BlockingIOError:
            return
        except OSError:
            return  # the evdev reader notices the disconnect and closes both
        if data:
            hub.hid_input(controller.id, data)

    try:
        while True:
            try:
                text = await asyncio.to_thread(Path("/proc/bus/input/devices").read_text)
                present = {c.id: c for c in list_controllers(text)}
                for controller_id in hub.open_ids() - set(present):
                    _close(loop, hub.closed(controller_id))
                for controller in present.values():
                    if controller.id in hub.open_ids():
                        continue
                    try:
                        f = open(_device_path(controller.event), "rb", buffering=0)
                    except OSError as exc:
                        logger.warning("gamepad: could not open %s: %s", controller.event, exc)
                        continue
                    os.set_blocking(f.fileno(), False)
                    codes = _abs_codes(f.fileno())
                    ranges = {code: r for code in codes if (r := _abs_range(f.fileno(), code))}
                    normaliser = Normaliser(layout_for(codes), ranges)
                    entry = _Open(controller, normaliser, f)
                    info = await asyncio.to_thread(hid_info, controller.event)
                    if info is not None:
                        node = info.pop("hidraw")
                        try:
                            hid_file = open(f"{os.environ.get('BRAGI_HOST_DEV', '/dev')}/{node}", "r+b", buffering=0)
                            os.set_blocking(hid_file.fileno(), False)
                            entry.hid, entry.hid_file = info, hid_file
                            loop.add_reader(hid_file.fileno(), on_report, controller, hid_file)
                        except OSError as exc:
                            # Usually the udev rule (host/udev/73-bragi-bluetooth-hidraw.rules).
                            logger.warning("gamepad: no passthrough for %s, %s: %s", controller.name, node, exc)
                    loop.add_reader(f.fileno(), on_event, controller, normaliser, f)
                    hub.opened(entry)
                    logger.info("gamepad: %s (%s) on %s%s", controller.name, controller.id, controller.event,
                                ", passthrough ready" if entry.hid_file else "")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("gamepad rescan failed")
            await asyncio.sleep(_RESCAN_SECONDS)
    finally:
        for controller_id in list(hub.open_ids()):
            _close(loop, hub.closed(controller_id))
