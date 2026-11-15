"""HyperX Cloud III S Wireless controls over the dongle's vendor HID
interface: battery, charging, mic monitor (sidetone), auto-off and EQ.

The protocol, from HeadsetControl issue 486
(https://github.com/Sapd/HeadsetControl/issues/486, NGENUITY captures),
HyperHeadset's cloud_iii_s_wireless.rs
(https://github.com/LennardKittner/HyperHeadset) and HaloBattery
(https://github.com/HeyOkay/HaloBattery), checked read-only against the
real headset on the Pi:

- Every request is a 64-byte *output* report on interface 3's hidraw
  node, `0C 02 03 <rw> 00 <op> <params...>`, rw 01 read, 00 write. The
  dongle ignores the same bytes sent as a Feature report.
- Replies come back as input report 0x0C echoing bytes 0-5, the value from
  byte 6. Every program with the node open sees every reply (another
  service on the Pi reads the battery this same way), so a reply only
  counts here if this process asked for that op and is still waiting.
- The headset also pushes input report 0x0D on its own:
  `0D 02 03 00 <kind> <value>`.

On Linux the dongle only answers at all if the kernel read its full
configuration descriptor at the first enumeration after power-up. Linux
asks for 9 bytes first, Windows for 255, and after a 9-byte request the
dongle stops forwarding anything from the headset until it is unplugged:
reads come back 0xFF or not at all, and nothing here can fix that from
userspace. The usbcore quirk `03f0:06be:r` (HyperHeadset issue 36) makes
Linux ask for 255. See docs/deployment.md.

Inside the container the node is opened from the host's /dev, bind-mounted
at BRAGI_HOST_DEV, because hidraw numbers change on replug and compose's
`devices:` only copies the nodes that exist when the container starts
(docker-compose.yml has the rest).

This shares interface 3 with the knob watcher, which reads the same
dongle's Consumer Control evdev node. Opening the hidraw node takes
nothing from that: every open file gets its own copy of each report.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml

from . import peers

logger = logging.getLogger("bragi.headset_hid")

VENDOR_ID = 0x03F0
# The 06BE dongle is the one on the Pi. 02CC is the other hardware revision
# of the same headset, listed by HyperHeadset and HaloBattery.
PRODUCT_IDS = (0x06BE, 0x02CC)
_HID_IDS = {f"0003:{VENDOR_ID:08X}:{pid:08X}" for pid in PRODUCT_IDS}

REPORT = 0x0C
NOTIFICATION = 0x0D
_PREFIX = bytes([REPORT, 0x02, 0x03])
_NOTIFICATION_PREFIX = bytes([NOTIFICATION, 0x02, 0x03])
_READ = 0x01
_WRITE = 0x00

OP_LINK = 0x02
OP_BATTERY = 0x06
OP_CHARGING = 0x48
# Mic monitor (sidetone) reads and writes on different ops. HeadsetControl
# issue 486 calls 0x16 a status poll, since NGENUITY sends it while idle.
# HyperHeadset reads it as the sidetone state. The Pi's headset answers it
# with 00 in byte 6, but nobody has yet shown it flipping to 01 after a
# 0x0D write. If it doesn't, the dialog shows mic monitor as off after
# turning it on.
OP_MIC_MONITOR_READ = 0x16
OP_MIC_MONITOR_WRITE = 0x0D
# Auto-off (sleep) timer, seconds big-endian. Read 0x4B, write 0x4A.
OP_AUTO_OFF_READ = 0x4B
OP_AUTO_OFF_WRITE = 0x4A
# NGENUITY's "Never" (HeadsetControl issue 486's sleeptimer capture).
# HyperHeadset sends 00 00 for "disabled", but no capture shows that
# working, so this sends what NGENUITY sends and reads either as never.
AUTO_OFF_NEVER = 0x7AC0
# HyperHeadset's choices, minus its 0. NGENUITY's own menu isn't captured.
AUTO_OFF_MINUTES = (5, 10, 15, 20, 30, 40, 60)
# EQ, one write per band: 5F <band> <gain hi> <gain lo>, gain signed 16-bit
# centi-dB, and band 9's report carries a commit byte (NGENUITY's
# eq-preset capture). The firmware takes one band per report. There is no
# EQ read in any source, so Bragi keeps the last EQ it set (EQ_FILE).
OP_EQ = 0x5F
EQ_BANDS_HZ = (32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
# HyperHeadset's limit. No capture shows NGENUITY's own.
EQ_LIMIT_DB = 12.0
# HyperHeadset waits this long between band reports, after watching
# NGENUITY do the same.
_EQ_REPORT_GAP_SECONDS = 0.003

EQ_FILE = peers.DATA_DIR / "headset_eq.yaml"
_eq_lock = threading.Lock()

# Notification kinds (byte 4 of report 0x0D), value in byte 5.
NOTIFY_BATTERY = 0x01
NOTIFY_MIC_MONITOR = 0x05
NOTIFY_CHARGING = 0x0A
NOTIFY_LINK = 0x0C

_CHARGING = {0: "no", 1: "charging", 2: "full"}

OnChange = Callable[[str], None]


def find_devices(sysfs_root: Path = Path("/sys/class/hidraw")) -> dict[str, str]:
    """{'hidrawN': USB serial} for each dongle present. All of interface
    3's collections, the vendor one included, share one hidraw node, so
    there's one per dongle. Found by USB id on every scan, never a fixed
    hidrawN: the numbers follow plug order."""
    found: dict[str, str] = {}
    try:
        entries = sorted(sysfs_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            uevent = (entry / "device" / "uevent").read_text()
        except OSError:
            continue
        fields = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        if fields.get("HID_ID", "").upper() in _HID_IDS:
            found[entry.name] = fields.get("HID_UNIQ", "")
    return found


def serial_matches(card_id: str, serial: str) -> bool:
    """'usb-HP__Inc_HyperX_Cloud_III_S_Wireless_C1V5340J99-00' carries the
    dongle's serial just before the interface suffix."""
    return bool(serial) and re.search(rf"_{re.escape(serial)}-\d+$", card_id) is not None


def read_request(op: int) -> bytes:
    return (_PREFIX + bytes([_READ, 0x00, op])).ljust(64, bytes(1))


def write_request(op: int, params: bytes) -> bytes:
    return (_PREFIX + bytes([_WRITE, 0x00, op]) + params).ljust(64, bytes(1))


def eq_requests(gains_db: list[float]) -> list[bytes]:
    reports = []
    for band, gain in enumerate(gains_db):
        centi = round(gain * 100).to_bytes(2, "big", signed=True)
        commit = bytes([1]) if band == len(EQ_BANDS_HZ) - 1 else b""
        reports.append(write_request(OP_EQ, bytes([band]) + centi + commit))
    return reports


def _valid_eq(gains) -> list[float]:
    if not isinstance(gains, list) or len(gains) != len(EQ_BANDS_HZ):
        raise ValueError(f"EQ needs {len(EQ_BANDS_HZ)} band gains")
    for gain in gains:
        if type(gain) not in (int, float) or not -EQ_LIMIT_DB <= gain <= EQ_LIMIT_DB:
            raise ValueError(f"EQ gains are numbers from -{EQ_LIMIT_DB:g} to {EQ_LIMIT_DB:g} dB")
    return [round(float(g), 2) for g in gains]


def _load_eq() -> dict:
    if not EQ_FILE.exists():
        return {}
    return yaml.safe_load(EQ_FILE.read_text()) or {}


def _saved_eq(serial: str) -> list[float] | None:
    with _eq_lock:
        return _load_eq().get(serial)


def _save_eq(serial: str, gains: list[float]) -> None:
    with _eq_lock:
        data = _load_eq()
        data[serial] = gains
        EQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        EQ_FILE.write_text(yaml.safe_dump(data, sort_keys=False))


class HeadsetOff(RuntimeError):
    """The dongle is there but the headset isn't on, so a setting would
    go nowhere."""


class HidHeadset:
    """One headset's state, fed by every report its hidraw node delivers.

    Owns no I/O itself: `write` sends one 64-byte report, and whoever reads
    the node calls handle_report() with each input report, which keeps this
    testable without a device. on_change(serial) runs after a report
    changed what view() returns."""

    def __init__(self, serial: str, write: Callable[[bytes], None], on_change: OnChange) -> None:
        self.serial = serial
        self._write = write
        self._on_change = on_change
        self._pending: set[int] = set()
        self._state: dict = {"headset_on": None, "battery": None, "millivolts": None, "charging": None,
                             "mic_monitor": None, "auto_off_known": False, "auto_off_minutes": None,
                             "eq": _saved_eq(serial)}
        # Set when the headset comes back on: the caller owns the I/O
        # timing, so it does the refresh (see supervise()).
        self.wants_refresh = False

    def view(self) -> dict:
        return dict(self._state)

    def _read(self, op: int) -> None:
        # Marked before writing: a fast reply can land before write() returns.
        self._pending.add(op)
        self._write(read_request(op))

    def refresh(self) -> None:
        self.wants_refresh = False
        for op in (OP_LINK, OP_BATTERY, OP_CHARGING, OP_MIC_MONITOR_READ, OP_AUTO_OFF_READ):
            self._read(op)

    def _require_on(self) -> None:
        if self._state["headset_on"] is False:
            raise HeadsetOff(f"headset {self.serial} is off")

    def set_mic_monitor(self, on: bool) -> None:
        """Read back afterwards rather than assumed: the reply is what the
        headset actually did."""
        self._require_on()
        self._write(write_request(OP_MIC_MONITOR_WRITE, bytes([1 if on else 0])))
        self._read(OP_MIC_MONITOR_READ)

    def set_auto_off(self, minutes: int | None) -> None:
        """minutes None is never. Read back afterwards, like the mic monitor."""
        self._require_on()
        if minutes is None:
            seconds = AUTO_OFF_NEVER
        elif type(minutes) is int and minutes in AUTO_OFF_MINUTES:
            seconds = minutes * 60
        else:
            raise ValueError(f"auto-off must be one of {AUTO_OFF_MINUTES} minutes, or never")
        self._write(write_request(OP_AUTO_OFF_WRITE, seconds.to_bytes(2, "big")))
        self._read(OP_AUTO_OFF_READ)

    def set_eq(self, gains_db: list[float]) -> None:
        """Kept as "what Bragi last set" once every band was written, since
        the headset can't be asked. A setting changed elsewhere (NGENUITY on
        another computer) won't show here."""
        self._require_on()
        gains = _valid_eq(gains_db)
        for i, report in enumerate(eq_requests(gains)):
            if i:
                time.sleep(_EQ_REPORT_GAP_SECONDS)
            self._write(report)
        _save_eq(self.serial, gains)
        self._state["eq"] = gains

    def handle_report(self, data: bytes) -> None:
        before = self.view()
        if len(data) >= 7 and data[:3] == _PREFIX and data[3] == _READ:
            self._handle_reply(data[5], data[6:])
        elif len(data) >= 6 and data[:4] == _NOTIFICATION_PREFIX + bytes(1):
            self._handle_notification(data[4], data[5])
        if self.view() != before:
            self._on_change(self.serial)

    def _handle_reply(self, op: int, value: bytes) -> None:
        if op not in self._pending:
            return
        self._pending.discard(op)
        if op == OP_LINK:
            self._set_headset_on(value[0] == 2)
        elif op == OP_BATTERY:
            if 0 < value[0] <= 100:
                self._state["battery"] = value[0]
                self._state["millivolts"] = int.from_bytes(value[1:3], "big") or None
            else:
                self._state["battery"] = None
                self._state["millivolts"] = None
        elif op == OP_CHARGING:
            self._state["charging"] = _CHARGING.get(value[0])
        elif op == OP_AUTO_OFF_READ and value[0] != 0xFF:
            seconds = int.from_bytes(value[0:2], "big")
            self._state["auto_off_known"] = True
            self._state["auto_off_minutes"] = None if seconds in (0, AUTO_OFF_NEVER) else round(seconds / 60)
        elif op == OP_MIC_MONITOR_READ and value[0] in (0, 1):
            self._state["mic_monitor"] = value[0] == 1

    def _handle_notification(self, kind: int, value: int) -> None:
        if kind == NOTIFY_BATTERY and 0 < value <= 100:
            self._state["battery"] = value
        elif kind == NOTIFY_MIC_MONITOR and value in (0, 1):
            self._state["mic_monitor"] = value == 1
        elif kind == NOTIFY_CHARGING:
            self._state["charging"] = _CHARGING.get(value)
        elif kind == NOTIFY_LINK:
            if value == 1 and self._state["headset_on"] is not True:
                self.wants_refresh = True
            self._set_headset_on(value == 1)

    def _set_headset_on(self, on: bool) -> None:
        self._state["headset_on"] = on
        if not on:
            for name in ("battery", "millivolts", "charging"):
                self._state[name] = None


# serial -> the headset on that dongle, for each dongle supervise() has open.
_devices: dict[str, HidHeadset] = {}


def attach(serial: str, write: Callable[[bytes], None], on_change: OnChange) -> HidHeadset:
    headset = HidHeadset(serial, write, on_change)
    _devices[serial] = headset
    return headset


def detach(serial: str) -> None:
    _devices.pop(serial, None)


def for_card(card_id: str) -> HidHeadset | None:
    """The HID side of the headset whose ALSA card id is card_id, if its
    dongle is one of these and is open."""
    return next((hs for serial, hs in _devices.items() if serial_matches(card_id, serial)), None)


def view_for(card_id: str) -> dict | None:
    headset = for_card(card_id)
    return headset.view() if headset is not None else None


HOST_DEV = Path(os.environ.get("BRAGI_HOST_DEV", "/dev"))
_RESCAN_SECONDS = 5
# The headset pushes battery and charging changes itself. This catches
# what it doesn't push, such as an auto-off change made from another
# computer.
_POLL_SECONDS = 60


async def supervise(on_change: Callable[[str], Awaitable[None]]) -> None:
    """Runs forever, like knob_watcher.watch(). Rescans sysfs every few
    seconds, opens each dongle that appears, polls it, and closes it when
    it goes. on_change(serial) is awaited whenever a headset's view changes
    or its dongle comes or goes. With no such dongle it does nothing and
    logs nothing."""
    loop = asyncio.get_running_loop()
    open_nodes: dict[str, tuple[int, str]] = {}  # 'hidrawN' -> (fd, serial)
    last_poll: dict[str, float] = {}
    warned: set[str] = set()

    def notify(serial: str) -> None:
        loop.create_task(on_change(serial))

    def close(node: str) -> None:
        fd, serial = open_nodes.pop(node)
        last_poll.pop(node, None)
        loop.remove_reader(fd)
        os.close(fd)
        detach(serial)
        logger.info("closed %s (dongle %s)", node, serial)
        notify(serial)

    def on_readable(node: str, fd: int, headset: HidHeadset) -> None:
        while node in open_nodes:
            try:
                data = os.read(fd, 64)
            except BlockingIOError:
                return
            except OSError:
                close(node)  # unplugged: ENODEV, before the rescan notices
                return
            if not data:
                return
            headset.handle_report(data)

    try:
        while True:
            try:
                present = await asyncio.to_thread(find_devices)
                for node in list(open_nodes):
                    if present.get(node) != open_nodes[node][1]:
                        close(node)
                for node, serial in present.items():
                    if node in open_nodes:
                        continue
                    if not serial:
                        if node not in warned:
                            logger.warning("%s has no USB serial, so there's no headset card to show it on", node)
                            warned.add(node)
                        continue
                    try:
                        fd = os.open(HOST_DEV / node, os.O_RDWR | os.O_NONBLOCK)
                    except OSError as exc:
                        if node not in warned:
                            # EPERM is the container's device cgroup (see
                            # docker-compose.yml), EACCES the node's mode
                            # (host/udev/), ENOENT a missing /dev mount.
                            logger.warning("cannot open %s for dongle %s: %s", HOST_DEV / node, serial, exc)
                            warned.add(node)
                        continue
                    warned.discard(node)
                    headset = attach(serial, lambda data, fd=fd: os.write(fd, data), notify)
                    loop.add_reader(fd, on_readable, node, fd, headset)
                    open_nodes[node] = (fd, serial)
                    last_poll[node] = float("-inf")
                    logger.info("opened %s for dongle %s", node, serial)
                    notify(serial)

                now = loop.time()
                for node, (_fd, serial) in list(open_nodes.items()):
                    headset = _devices.get(serial)
                    if headset is None or not (headset.wants_refresh or now - last_poll[node] >= _POLL_SECONDS):
                        continue
                    last_poll[node] = now
                    try:
                        await asyncio.to_thread(headset.refresh)
                    except OSError:
                        if node in open_nodes:
                            close(node)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("headset HID rescan failed, retrying in %ss", _RESCAN_SECONDS)

            await asyncio.sleep(_RESCAN_SECONDS)
    finally:
        for node in list(open_nodes):
            fd, serial = open_nodes.pop(node)
            loop.remove_reader(fd)
            os.close(fd)
            detach(serial)
