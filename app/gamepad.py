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


class Hub:
    """The open controllers, the target, and what goes to which client."""

    def __init__(self, send, broadcast) -> None:
        # send(peer, message, droppable) -> None; broadcast(message) -> None
        self._send = send
        self._broadcast = broadcast
        self._open: dict[str, tuple[Controller, Normaliser, object]] = {}
        self._target = target()

    def status(self) -> dict:
        return {"type": "gamepads", "target": self._target,
                "controllers": [{"id": c.id, "name": c.name} for c, _, _ in self._open.values()]}

    def set_target(self, name: str | None) -> None:
        if name == self._target:
            return
        old, self._target = self._target, name
        _save_target(name)
        for controller, normaliser, _ in self._open.values():
            if old:
                self._send(old, {"type": "gamepad_detach", "id": controller.id}, False)
            if name:
                self._send(name, {"type": "gamepad", "id": controller.id, **normaliser.state()}, False)
        self._broadcast(self.status())
        logger.info("gamepad: target %s -> %s", old, name)

    def release(self, name: str) -> None:
        if self._target == name:
            self.set_target(None)

    def opened(self, controller: Controller, normaliser: Normaliser, handle) -> None:
        self._open[controller.id] = (controller, normaliser, handle)
        if self._target:
            self._send(self._target, {"type": "gamepad", "id": controller.id, **normaliser.state()}, False)
        self._broadcast(self.status())

    def closed(self, controller_id: str) -> object | None:
        entry = self._open.pop(controller_id, None)
        if entry is None:
            return None
        if self._target:
            self._send(self._target, {"type": "gamepad_detach", "id": controller_id}, False)
        self._broadcast(self.status())
        return entry[2]

    def input(self, controller_id: str) -> None:
        entry = self._open.get(controller_id)
        if entry and self._target:
            # Droppable: a newer state replaces it if the socket falls behind.
            self._send(self._target, {"type": "gamepad", "id": controller_id, **entry[1].state()}, True)

    def open_ids(self) -> set[str]:
        return set(self._open)


hub: Hub | None = None


async def supervise(send, broadcast) -> None:
    """Runs forever: opens controllers as they appear, sends their input to
    the target, and closes them when they go."""
    global hub
    hub = Hub(send, broadcast)
    loop = asyncio.get_running_loop()

    def on_readable(controller: Controller, normaliser: Normaliser, f) -> None:
        try:
            data = f.read(_EVENT_SIZE * 64)
        except BlockingIOError:
            return
        except OSError:  # ENODEV: the controller disconnected
            data = b""
        if data is None:  # non-blocking read with nothing waiting
            return
        if not data:
            handle = hub.closed(controller.id)
            if handle is not None:
                loop.remove_reader(handle.fileno())
                handle.close()
                logger.info("gamepad: %s (%s) went away", controller.name, controller.id)
            return
        report = False
        for offset in range(0, len(data) - _EVENT_SIZE + 1, _EVENT_SIZE):
            _s, _u, etype, code, value = struct.unpack_from(_EVENT_FMT, data, offset)
            report = normaliser.feed(etype, code, value) or report
        if report:
            hub.input(controller.id)

    try:
        while True:
            try:
                text = await asyncio.to_thread(Path("/proc/bus/input/devices").read_text)
                present = {c.id: c for c in list_controllers(text)}
                for controller_id in hub.open_ids() - set(present):
                    handle = hub.closed(controller_id)
                    if handle is not None:
                        loop.remove_reader(handle.fileno())
                        handle.close()
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
                    loop.add_reader(f.fileno(), on_readable, controller, normaliser, f)
                    hub.opened(controller, normaliser, f)
                    logger.info("gamepad: %s (%s) on %s", controller.name, controller.id, controller.event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("gamepad rescan failed")
            await asyncio.sleep(_RESCAN_SECONDS)
    finally:
        for controller_id in list(hub.open_ids()):
            handle = hub.closed(controller_id)
            if handle is not None:
                loop.remove_reader(handle.fileno())
                handle.close()
