"""Which USB devices share a full-speed lane (app/usb_topology.py, bragi#63).
The fake sysfs is sagehub's: two OHCI buses and a USB 2.0 hub on EHCI."""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import setup_checks, usb_topology
from app.main import app

# sysfs interface directories are named like "4-1:1.0", which Windows can't create.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs ':' in file names")


def device(base, name, speed, product="", cls="00", interfaces=(), proto=None):
    d = base / name
    d.mkdir(parents=True)
    for attr, value in (("busnum", name.split("-")[0].removeprefix("usb")), ("speed", speed), ("product", product),
                        ("bDeviceClass", cls), ("idVendor", "0000"), ("idProduct", "0000")):
        (d / attr).write_text(value + "\n")
    for i, c in enumerate(interfaces):
        iface = base / f"{name}:1.{i}"
        iface.mkdir()
        (iface / "bInterfaceClass").write_text(c + "\n")
        if proto:
            (iface / "bInterfaceProtocol").write_text(proto + "\n")


@pytest.fixture
def sagehub(tmp_path, monkeypatch):
    base = tmp_path / "bus/usb/devices"
    device(base, "usb1", "480", "EHCI Host Controller", "09")
    device(base, "usb4", "12", "OHCI PCI host controller", "09")
    device(base, "usb5", "12", "OHCI PCI host controller", "09")
    device(base, "4-1", "12", "CSR8510 A10", "e0", ["e0"])
    device(base, "4-2", "12", "AT2020USB+", "00", ["01", "01", "01", "03"])
    device(base, "4-5", "12", "HyperX Cloud III S Wireless", "00", ["01", "01", "01", "03"])
    device(base, "5-1", "12", "HyperX Cloud III Wireless", "00", ["01", "01", "01", "03"])
    device(base, "1-4", "480", "USB2.1 Hub", "09", ["09"], proto="01")
    device(base, "1-4.1", "480", "A6210", "00", ["ff"])
    device(base, "1-4.2", "12", "TrackPoint Keyboard II", "00", ["03", "03"])
    device(base, "1-4.4", "12", "USB Receiver", "00", ["03", "03", "03"])
    monkeypatch.setattr(usb_topology, "SYS", tmp_path)
    monkeypatch.setattr(usb_topology, "_driver", lambda root: "ohci-pci" if root.name in ("usb4", "usb5") else "ehci-pci")
    log = {"stdout": "", "rc": 1}
    monkeypatch.setattr(usb_topology.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, log["rc"], log["stdout"], ""))
    return {"base": base, "log": log}


def test_devices_are_grouped_by_lane(sagehub):
    lanes = {l["lane"]: l for l in usb_topology.lanes()}
    assert set(lanes) == {"bus 4 (ohci-pci)", "bus 5 (ohci-pci)", "hub 1-4 (USB2.1 Hub)"}
    assert [d["product"] for d in lanes["bus 4 (ohci-pci)"]["devices"]] == ["CSR8510 A10", "AT2020USB+", "HyperX Cloud III S Wireless"]
    assert lanes["bus 4 (ohci-pci)"]["audio"] == 2 and lanes["bus 5 (ohci-pci)"]["audio"] == 1
    # Receivers share the hub's single translator; the high-speed Wi-Fi dongle uses no lane.
    assert [d["product"] for d in lanes["hub 1-4 (USB2.1 Hub)"]["devices"]] == ["TrackPoint Keyboard II", "USB Receiver"]
    assert usb_topology.lanes()[0]["lane"] == "bus 4 (ohci-pci)"  # busiest first


def test_a_multi_tt_hub_gives_each_port_its_own_lane(sagehub):
    (sagehub["base"] / "1-4:1.0" / "bInterfaceProtocol").write_text("02\n")
    lanes = {l["lane"] for l in usb_topology.lanes()}
    assert {"hub 1-4 port 2", "hub 1-4 port 4"} <= lanes


def test_a_third_audio_device_on_a_lane_is_a_problem(sagehub):
    assert setup_checks.check_usb()["status"] == "ok" and "Two audio devices share bus 4" in setup_checks.check_usb()["detail"]
    device(sagehub["base"], "4-4", "12", "HyperX Cloud III Wireless", "00", ["01", "01", "01", "03"])
    c = setup_checks.check_usb()
    assert c["status"] == "problem" and "3 audio devices share bus 4" in c["detail"]


def test_bandwidth_refusals_from_the_kernel_log(sagehub):
    sagehub["log"].update(rc=0, stdout="usb 5-1: cannot submit urb 0, error -28: not enough bandwidth\n" * 3
                          + "usb 9-9: Not enough bandwidth for new device state.\n")
    view = usb_topology.view()
    assert view["failing"][0] == {"name": "5-1", "product": "HyperX Cloud III Wireless", "count": 3,
                                  "lane": "bus 5 (ohci-pci)"}
    assert view["failing"][1]["product"] == "nothing there now"
    c = setup_checks.check_usb()
    assert c["status"] == "problem" and "refused USB bandwidth 3 times" in c["detail"]


def test_the_settings_section(sagehub, session):
    page = TestClient(app).get("/usb").text
    assert "bus 4 (ohci-pci)" in page and "AT2020USB+ (audio)" in page and "no room for another" in page
