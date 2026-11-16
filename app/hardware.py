"""The device inventory: every USB and Bluetooth device on the box, what
kind it is, which module looks after it, and its battery where known.

Sources, merged into one list:
- /proc/bus/input/devices: input devices, grouped into physical devices
  (USB by port path, Bluetooth by address)
- the headset dongles Bragi's HID code has open (battery, charging)
- the KVM engine's status: devices paired through a Logitech receiver
  (HID++), with their battery
- BlueZ: paired Bluetooth devices, connected or not
- the kernel's batteries (/sys/class/power_supply): USB and Bluetooth HID
  devices that report one, like the TrackPoint Keyboard II

A device reports its battery only now and then, and the kernel answers
nothing in between, so the last value seen is kept (with when it was
seen) in data/batteries.json and shown until a newer one arrives. Below
LOW_PERCENT a battery is marked low.

The list is read-only and built on request; the API serves it at
/api/v1/hardware.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from . import kvm, peers

LOW_PERCENT = 20
BATTERIES_FILE = peers.DATA_DIR / "batteries.json"
_battery_lock = threading.Lock()

_BUSES = {"0003": "usb", "0005": "bluetooth"}


def _blocks(text: str):
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if len(line) > 3 and line[1] == ":":
                fields.setdefault(line[0], "")
                fields[line[0]] += line[3:] + "\n"
        if fields:
            yield fields


def input_devices(text: str) -> list[dict]:
    """Physical USB and Bluetooth devices from /proc/bus/input/devices,
    each with the kinds its input nodes add up to."""
    found: dict[str, dict] = {}
    for f in _blocks(text):
        ids = dict(re.findall(r"(\w+)=(\w+)", f.get("I", "")))
        bus = _BUSES.get(ids.get("Bus", ""))
        if bus is None:
            continue  # HDMI CEC, virtual devices, the Pi's own buttons
        name = (re.search(r'Name="(.*)"', f.get("N", "")) or [None, ""])[1]
        phys = (re.search(r"Phys=(\S*)", f.get("P", "")) or [None, ""])[1]
        uniq = (re.search(r"Uniq=(\S*)", f.get("U", "")) or [None, ""])[1].lower()
        handlers = f.get("H", "")
        # One physical device has several input nodes: same USB port path
        # (minus /inputN), or for Bluetooth the same device address.
        key = f"bt:{uniq}" if bus == "bluetooth" and uniq else f"usb:{phys.rsplit('/input', 1)[0]}"
        device = found.setdefault(key, {
            "id": key, "name": name, "bus": bus, "vendor": ids.get("Vendor", ""),
            "product": ids.get("Product", ""), "kinds": set(), "connected": True, "battery": None,
        })
        if re.search(r"\bjs\d+", handlers):
            device["kinds"].add("controller")
        if re.search(r"\bmouse\d+", handlers):
            device["kinds"].add("mouse")
        if "leds" in handlers and "kbd" in handlers:
            device["kinds"].add("keyboard")
        # The shortest name is usually the device's own ("... Keyboard II" over "... Mouse").
        if len(name) < len(device["name"]):
            device["name"] = name
    return list(found.values())


def _port(path: str) -> str | None:
    """The USB port ("1-1.1.2") a sysfs path runs through, or a Bluetooth
    HID device's id ("0005:2DC8:901B")."""
    ports = re.findall(r"/(\d+-[\d.]+)(?=[/:])", path)
    if ports:
        return ports[-1]
    bt = re.search(r"/(0005:[0-9A-F]{4}:[0-9A-F]{4})\.", path)
    return bt.group(1) if bt else None


def kernel_batteries(sys_root: str = "/sys") -> list[dict]:
    """Device batteries the kernel knows about, as they read right now:
    {key, model, percent or None, charging or None}."""
    found = []
    for ps in sorted((Path(sys_root) / "class" / "power_supply").glob("*")):
        def attr(name, ps=ps):
            try:
                return (ps / name).read_text().strip()
            except OSError:  # ENODATA until the device has reported
                return None
        if attr("type") != "Battery" or attr("scope") != "Device":
            continue
        port = _port(os.path.realpath(ps / "device"))
        if port is None:
            continue
        capacity = attr("capacity")
        status = (attr("status") or "").lower()
        found.append({"key": port, "model": attr("model_name") or ps.name,
                      "percent": int(capacity) if capacity and capacity.isdigit() else None,
                      "charging": {"charging": True, "discharging": False, "not charging": False,
                                   "full": False}.get(status)})
    return found


def _remembered(live: list[dict], now: float | None = None) -> dict[str, dict]:
    """Merges what the kernel says now with the last values seen, saves the
    result, and returns {key: {percent, charging, seen}}."""
    now = time.time() if now is None else now
    with _battery_lock:
        try:
            known = json.loads(BATTERIES_FILE.read_text())
        except (OSError, ValueError):
            known = {}
        changed = False
        for battery in live:
            if battery["percent"] is None:
                continue
            entry = {"percent": battery["percent"], "charging": battery["charging"], "seen": int(now)}
            if known.get(battery["key"], {}).get("percent") != entry["percent"] or \
                    known.get(battery["key"], {}).get("charging") != entry["charging"]:
                changed = True
            known[battery["key"]] = entry
        if changed:
            try:
                BATTERIES_FILE.parent.mkdir(parents=True, exist_ok=True)
                tmp = BATTERIES_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(known, indent=2))
                tmp.replace(BATTERIES_FILE)
            except OSError:
                pass
        return known


async def watch_batteries(interval: float = 60) -> None:
    """Runs forever: records every battery value the kernel reports, so the
    inventory has one even while a device says nothing."""
    import asyncio
    while True:
        try:
            live = await asyncio.to_thread(kernel_batteries)
            await asyncio.to_thread(_remembered, live)
        except asyncio.CancelledError:
            raise
        except Exception:  # never let a sysfs oddity stop the loop
            pass
        await asyncio.sleep(interval)


def _battery(percent, charging, seen=None) -> dict | None:
    if percent is None:
        return None
    battery = {"percent": percent, "charging": bool(charging) if charging is not None else None,
               "low": percent <= LOW_PERCENT and not charging}
    if seen is not None:
        battery["seen"] = seen
    return battery


def _owner(device: dict, modules: dict[str, bool]) -> str | None:
    kinds = device["kinds"]
    if "headset" in kinds:
        return "audio" if modules.get("audio") else None
    if "controller" in kinds:
        return "controllers" if modules.get("controllers") else None
    if kinds & {"keyboard", "mouse"}:
        return "kvm" if modules.get("kvm") else None
    return None


def inventory(modules: dict[str, bool], proc_text: str | None = None, headsets: list[dict] | None = None,
              engine_status: dict | None = None, bluetooth: list | None = None,
              batteries: list[dict] | None = None, now: float | None = None) -> list[dict]:
    """The merged list. Arguments default to reading the live sources."""
    if proc_text is None:
        try:
            proc_text = Path("/proc/bus/input/devices").read_text()
        except OSError:
            proc_text = ""
    devices = input_devices(proc_text)

    for headset in headsets if headsets is not None else _live_headsets():
        # The dongle's input nodes (its knob) are already listed by vendor:product.
        match = next((d for d in devices if d["bus"] == "usb" and d["vendor"].lower() == headset["vendor"]
                      and d["product"].lower() == headset["product"]), None)
        if match is None:
            match = {"id": f"usb:headset:{headset['serial']}", "name": headset["name"], "bus": "usb",
                     "vendor": headset["vendor"], "product": headset["product"], "kinds": set(),
                     "connected": True, "battery": None}
            devices.append(match)
        match["kinds"] = {"headset"}
        match["name"] = headset["name"]
        b = headset.get("battery") or {}
        # The headset's own protocol says "yes"/"no" for charging.
        charging = b.get("charging")
        match["battery"] = _battery(b.get("percent"), charging in (True, "yes") if charging is not None else None)

    if engine_status is None and (own := _live_hidpp()) is not None:
        status = {"hidpp": own}
    else:
        status = engine_status if engine_status is not None else _live_engine_status()
    for hidpp in (status or {}).get("hidpp", []):
        battery = hidpp.get("battery") or {}
        devices.append({"id": f"receiver:{hidpp.get('device', '')}", "name": hidpp.get("device", ""),
                        "bus": "receiver", "vendor": "046d", "product": "", "kinds": {"mouse"},
                        "connected": True,
                        "battery": _battery(battery.get("percent"), battery.get("state") == "charging")
                        if battery else None})

    for paired in bluetooth if bluetooth is not None else _live_bluetooth():
        key = f"bt:{paired.address.lower()}"
        match = next((d for d in devices if d["id"] == key), None)
        if match is None:
            kind = {"controller": "controller", "keyboard": "keyboard", "mouse": "mouse"}.get(paired.kind)
            devices.append({"id": key, "name": paired.name, "bus": "bluetooth", "vendor": "", "product": "",
                            "kinds": {kind} if kind else set(), "connected": paired.connected, "battery": None})

    # Kernel batteries, remembered: matched to a USB device by its port,
    # to a Bluetooth device by vendor and product.
    remembered = _remembered(batteries if batteries is not None else kernel_batteries(), now)
    for key, value in remembered.items():
        for device in devices:
            if device["battery"] is not None:
                continue
            if device["bus"] == "usb" and device["id"].startswith("usb:usb-") and \
                    device["id"].rsplit("-", 1)[-1] == key.split("-", 1)[-1]:
                device["battery"] = _battery(value["percent"], value["charging"], value["seen"])
            elif device["bus"] == "bluetooth" and key.startswith("0005:") and \
                    key[5:].lower() == f"{device['vendor']}:{device['product']}".lower():
                device["battery"] = _battery(value["percent"], value["charging"], value["seen"])

    for device in devices:
        device["owner"] = _owner(device, modules)
        device["kinds"] = sorted(device["kinds"])
    return sorted(devices, key=lambda d: (d["owner"] is None, d["bus"], d["name"].lower()))


def _live_headsets() -> list[dict]:
    from . import headset_hid
    result = []
    for serial in headset_hid.serials():
        device = headset_hid._devices.get(serial)
        view = device.view() if device is not None else {}
        result.append({"serial": serial, "name": "HyperX Cloud III S Wireless", "vendor": "03f0", "product": "06be",
                       "battery": {"percent": view.get("battery"), "charging": view.get("charging")}
                       if view.get("battery") is not None else None})
    return result


def _live_hidpp() -> list[dict] | None:
    """Bragi's own HID++ devices (hidpp.py), in the status file's shape, or
    None when it has no receiver open."""
    from . import hidpp
    if hidpp.manager is None or not hidpp.manager.links:
        return None
    return [{"device": d["name"], "battery": d["battery"]} for d in hidpp.manager.devices()]


def _live_engine_status() -> dict | None:
    try:
        return json.loads((kvm.engine_dir() / "status.json").read_text())
    except (OSError, ValueError):
        return None


def _live_bluetooth() -> list:
    from . import bluetooth
    if bluetooth.manager is None:
        return []
    return [d for d in bluetooth.manager.last[2] if d.paired]
