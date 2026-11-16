"""The device inventory: every USB and Bluetooth device on the box, what
kind it is, which module looks after it, and its battery where known.

Sources, merged into one list:
- /proc/bus/input/devices: input devices, grouped into physical devices
  (USB by port path, Bluetooth by address)
- the headset dongles Bragi's HID code has open (battery, charging)
- the KVM engine's status: devices paired through a Logitech receiver
  (HID++), with their battery
- BlueZ: paired Bluetooth devices, connected or not

The list is read-only and built on request; the API serves it at
/api/v1/hardware.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import kvm

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
              engine_status: dict | None = None, bluetooth: list | None = None) -> list[dict]:
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
        match["battery"] = headset.get("battery")

    status = engine_status if engine_status is not None else _live_engine_status()
    for hidpp in (status or {}).get("hidpp", []):
        battery = hidpp.get("battery") or {}
        devices.append({"id": f"receiver:{hidpp.get('device', '')}", "name": hidpp.get("device", ""),
                        "bus": "receiver", "vendor": "046d", "product": "", "kinds": {"mouse"},
                        "connected": True,
                        "battery": {"percent": battery.get("percent"), "charging": battery.get("state") == "charging"}
                        if battery else None})

    for paired in bluetooth if bluetooth is not None else _live_bluetooth():
        key = f"bt:{paired.address.lower()}"
        match = next((d for d in devices if d["id"] == key), None)
        if match is None:
            kind = {"controller": "controller", "keyboard": "keyboard", "mouse": "mouse"}.get(paired.kind)
            devices.append({"id": key, "name": paired.name, "bus": "bluetooth", "vendor": "", "product": "",
                            "kinds": {kind} if kind else set(), "connected": paired.connected, "battery": None})

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
