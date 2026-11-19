"""Which USB devices share bandwidth, and which ran out of it (bragi#63).

Headset dongles, USB mics and keyboard/mouse receivers are full-speed
(12 Mbit/s) devices, and full-speed devices share a lane:

- on a USB 1.1 controller (OHCI/UHCI root hub), the whole bus is one lane;
- behind a USB 2.0 hub, the hub's transaction translator: one lane for
  the whole hub (single-TT) or one per port (multi-TT);
- on a USB 3 (xHCI) root port, that port.

Audio devices reserve a share of their lane up front. With too many on
one lane the kernel refuses a stream ("cannot submit urb ... error -28:
not enough bandwidth") and the device just stays silent: a second HyperX
headset on sagehub failed 1,336 times that way before anyone looked, and
the Pi's single VL805 hub lane kept running out.

lanes() groups the devices by lane; recent_errors() reads the kernel log
for bandwidth refusals; view() puts both together for the dashboard.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

SYS = Path(os.environ.get("BRAGI_SYS", "/sys"))
AUDIO, HID, WIRELESS, HUB = "01", "03", "e0", "09"
CROWDED = 3  # audio devices on one lane that don't fit (seen on sagehub)
_ERROR = re.compile(r"usb (\d+-[\d.]+): .*(not enough bandwidth|usb_set_interface failed \(-28\))", re.I)


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _driver(root: Path) -> str:
    """The host controller's driver: ohci-pci, ehci-pci, xhci_hcd, dwc2…"""
    try:
        return Path(os.readlink(root / ".." / "driver")).name
    except OSError:
        return ""


def _parent(name: str) -> str:
    """'1-4.2' -> '1-4', '4-4' -> 'usb4'."""
    bus, port = name.split("-", 1)
    return f"{bus}-{port.rsplit('.', 1)[0]}" if "." in port else f"usb{bus}"


def _classes(dev: Path, name: str) -> set[str]:
    return {_read(i / "bInterfaceClass") for i in dev.parent.glob(f"{name}:*") if (i / "bInterfaceClass").exists()}


def devices() -> dict[str, dict]:
    """Every USB device and root hub by its sysfs name."""
    out = {}
    base = SYS / "bus/usb/devices"
    for dev in sorted(base.glob("*")):
        name = dev.name
        if ":" in name:
            continue
        out[name] = {
            "name": name,
            "bus": _read(dev / "busnum"),
            "speed": _read(dev / "speed"),
            "product": _read(dev / "product") or f"{_read(dev / 'idVendor')}:{_read(dev / 'idProduct')}",
            "classes": _classes(dev, name),
            "hub": _read(dev / "bDeviceClass") == HUB,
            "root": name.startswith("usb"),
            "driver": _driver(dev) if name.startswith("usb") else "",
            "multi_tt": _read(base / f"{name}:1.0" / "bInterfaceProtocol") == "02",
        }
    return out


def _lane(name: str, devs: dict[str, dict]) -> str | None:
    """Which full-speed lane a device uses, or None for a high-speed device."""
    me = devs[name]
    if me["speed"] not in ("1.5", "12"):
        return None
    child, parent = name, _parent(name)
    while parent in devs:
        p = devs[parent]
        if p["root"]:
            if p["speed"] in ("1.5", "12"):
                return f"bus {p['bus']} ({p['driver'] or 'USB 1.1'})"
            return f"bus {p['bus']} port {child.split('-', 1)[1].split('.')[0]}"
        if p["speed"] not in ("1.5", "12"):
            # A high-speed hub: its transaction translator is the lane.
            return f"hub {parent} port {child.rsplit('.', 1)[-1]}" if p["multi_tt"] else f"hub {parent} ({p['product']})"
        child, parent = parent, _parent(parent)
    return f"bus {me['bus']}"


def _kind(d: dict) -> str:
    if AUDIO in d["classes"]:
        return "audio"
    if WIRELESS in d["classes"]:
        return "wireless"
    if HID in d["classes"]:
        return "input"
    return "other"


def lanes(devs: dict[str, dict] | None = None) -> list[dict]:
    """[{"lane", "devices": [{"name", "product", "kind"}], "audio": n}], busiest first."""
    devs = devices() if devs is None else devs
    grouped: dict[str, list[dict]] = {}
    for name, d in devs.items():
        if d["root"] or d["hub"]:
            continue
        lane = _lane(name, devs)
        if lane is not None:
            grouped.setdefault(lane, []).append({"name": name, "product": d["product"], "kind": _kind(d)})
    out = [{"lane": lane, "devices": ds, "audio": sum(1 for d in ds if d["kind"] == "audio")}
           for lane, ds in grouped.items()]
    return sorted(out, key=lambda l: (-l["audio"], -len(l["devices"]), l["lane"]))


def recent_errors(minutes: int = 15) -> dict[str, int] | None:
    """Bandwidth refusals per device path in the kernel log, or None when it
    can't be read (the user needs the adm or systemd-journal group)."""
    try:
        res = subprocess.run(["journalctl", "-k", "-b", "--since", f"-{minutes}min", "--no-pager", "-o", "cat",
                              "-g", "bandwidth|usb_set_interface failed"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode not in (0, 1) or "No journal files" in res.stderr or "permission" in res.stderr.lower():
        return None
    counts: dict[str, int] = {}
    for line in res.stdout.splitlines():
        if m := _ERROR.search(line):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def view() -> dict:
    devs = devices()
    errors = recent_errors()
    failing = [{"name": n, "product": devs[n]["product"] if n in devs else "nothing there now",
                "count": c, "lane": _lane(n, devs) if n in devs else None}
               for n, c in sorted((errors or {}).items(), key=lambda e: -e[1])]
    return {"lanes": lanes(devs), "failing": failing, "log_readable": errors is not None, "crowded": CROWDED}
