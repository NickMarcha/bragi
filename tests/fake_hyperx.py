"""A fake HyperX Cloud III S Wireless dongle, substituted at the hidraw
write - the one place app/headset_hid.py talks to the device.

It answers read requests the way the real dongle does (captured on the
Pi: `0c 02 03 01 00 06 0f 0e 4e` for 15 % at 3662 mV), echoes writes, and
can push the unsolicited 0x0D notifications the headset sends on its own.
Replies go to `deliver`, which tests point at the controller's
handle_report - directly, or through the event loop for the async paths,
since on the Pi replies arrive on the loop's reader, never inside write().
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

SERIAL = "0000000001"  # matches fake_pipewire.HEADSET_CARD_ID


@dataclass
class FakeDongle:
    battery: int = 15
    millivolts: int = 3662
    charging: int = 0
    mic_monitor: bool = False
    auto_off: int = 1200
    headset_on: bool = True
    # Ops this dongle never answers, to model a dropped reply.
    silent_ops: set[int] = field(default_factory=set)
    writes: list[bytes] = field(default_factory=list)
    deliver: Callable[[bytes], None] | None = None

    def write(self, data: bytes) -> None:
        assert len(data) == 64, f"report is {len(data)} bytes, not 64"
        self.writes.append(bytes(data))
        if data[:3] != bytes([0x0C, 0x02, 0x03]):
            return
        rw, op = data[3], data[5]
        if op in self.silent_ops:
            return
        if rw == 0x00:
            if op == 0x0D:
                self.mic_monitor = data[6] == 1
            elif op == 0x4A:
                self.auto_off = (data[6] << 8) | data[7]
            self._send(data[:10])
            return
        self._send(data[:6] + self._value(op))

    def _value(self, op: int) -> bytes:
        if op == 0x02:
            return bytes([2 if self.headset_on else 0])
        if not self.headset_on:
            return bytes([0xFF])
        if op == 0x06:
            return bytes([self.battery]) + self.millivolts.to_bytes(2, "big")
        if op == 0x48:
            return bytes([self.charging])
        if op == 0x16:
            return bytes([1 if self.mic_monitor else 0])
        if op == 0x4B:
            return self.auto_off.to_bytes(2, "big")
        return bytes([0])

    def notify(self, kind: int, value: int) -> None:
        """An unsolicited input report 0x0D, as the headset sends it."""
        self._send(bytes([0x0D, 0x02, 0x03, 0x00, kind, value]))

    def press(self, code: int) -> None:
        """A button report and its release, as the headset sends them: 08,
        10 or 20 for the macro button's single, double or triple press, 01
        or 02 for the wheel."""
        self._send(bytes([0x0F, code]))
        self._send(bytes([0x0F, 0x00]))

    def reply_from_elsewhere(self, op: int) -> None:
        """A reply to a read some other program on the host sent."""
        self._send(bytes([0x0C, 0x02, 0x03, 0x01, 0x00, op]) + self._value(op))

    def _send(self, report: bytes) -> None:
        if self.deliver is not None:
            self.deliver(report.ljust(64, b"\0"))

    def sent(self, op: int) -> list[bytes]:
        return [w for w in self.writes if w[5] == op]
