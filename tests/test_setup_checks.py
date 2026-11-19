"""Settings > Setup (app/setup_checks.py, bragi#58): host checks and fixes.
The system is faked: commands through _run, /sys and /proc as tmp dirs."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import setup_checks as sc
from app.main import app


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host with everything set up; tests break one thing at a time."""
    sys_ = tmp_path / "sys"; proc = tmp_path / "proc"; state = tmp_path / "state"; linger = tmp_path / "linger"
    for d in (sys_ / "module/usbcore/parameters", sys_ / "bus/usb/devices/1-1", proc, state, linger):
        d.mkdir(parents=True)
    (sys_ / "bus/usb/devices/1-1/idVendor").write_text("03f0\n")
    (sys_ / "bus/usb/devices/1-1/idProduct").write_text("06be\n")
    (sys_ / "module/usbcore/parameters/quirks").write_text("03f0:06be:r\n")
    (proc / "cmdline").write_text("root=/dev/sda1 usbcore.quirks=03f0:06be:r quiet\n")
    (linger / "sage").write_text("")
    helper = tmp_path / "bragi-setup.path"; helper.write_text("")
    monkeypatch.setattr(sc, "SYS", sys_)
    monkeypatch.setattr(sc, "PROC", proc)
    monkeypatch.setattr(sc, "STATE_DIR", state)
    monkeypatch.setattr(sc, "LINGER_DIR", linger)
    monkeypatch.setattr(sc, "HELPER_UNIT", helper)
    monkeypatch.setattr(sc, "_user", lambda: "sage")
    monkeypatch.setattr(sc, "_module", lambda name: True)
    monkeypatch.setattr(sc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sc, "check_input_group", lambda: sc._check("input", "Input devices", "ok", "fake"))
    h = {"active": {}, "serve": {"TCP": {"443": {"HTTPS": True}}, "Web": {"sagehub:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}}},
         "started": [], "start_rc": 0, "dir": tmp_path}

    def run(cmd, timeout=5):
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return 0, "\n".join(h["active"].get(u, "active") for u in cmd[3:]) + "\n"
        if cmd[:3] == ["systemctl", "--user", "start"]:
            h["started"].append(cmd[3:])
            return h["start_rc"], ""
        if cmd[:3] == ["tailscale", "serve", "status"]:
            return 0, json.dumps(h["serve"])
        return 1, ""

    monkeypatch.setattr(sc, "_run", run)
    return h


def by_id():
    return {c["id"]: c for c in sc.run_all()}


def test_a_set_up_host_passes_every_check(host):
    checks = by_id()
    assert {i: c["status"] for i, c in checks.items()} == {
        "pipewire": "ok", "modules": "ok", "linger": "ok", "input": "ok",
        "hyperx": "ok", "tailscale": "ok", "services": "ok"}
    assert all(c["fix"] is None for c in checks.values())


def test_pipewire_not_running_offers_to_start_it(host):
    host["active"]["wireplumber"] = "inactive"
    c = by_id()["pipewire"]
    assert c["status"] == "problem" and c["fix"] == {"kind": "user", "action": "start-pipewire", "label": "Start them"}
    assert sc.fix("pipewire") == "Started."
    assert host["started"] == [["pipewire", "wireplumber", "pipewire-pulse"]]


def test_missing_roc_and_vban_modules_ask_the_helper_to_install_them(host, monkeypatch):
    monkeypatch.setattr(sc, "_module", lambda name: not name.startswith("roc"))
    c = by_id()["modules"]
    assert c["status"] == "problem" and "roc-sink, roc-source" in c["detail"]
    assert c["fix"]["action"] == "packages"
    sc.fix("modules")
    assert (host["dir"] / "state/setup-request").read_text() == "packages sage\n"


def test_lingering_off(host):
    (host["dir"] / "linger/sage").unlink()
    c = by_id()["linger"]
    assert c["status"] == "problem" and c["fix"]["action"] == "linger"


def test_hyperx_quirk_states(host):
    sys_, proc = host["dir"] / "sys", host["dir"] / "proc"
    (proc / "cmdline").write_text("root=/dev/sda1 quiet\n")
    c = by_id()["hyperx"]
    assert c["status"] == "ok" and "this boot" in c["detail"]          # set at runtime only
    (sys_ / "module/usbcore/parameters/quirks").write_text("\n")
    c = by_id()["hyperx"]
    assert c["status"] == "problem" and c["fix"]["action"] == "hyperx-quirk"
    (sys_ / "bus/usb/devices/1-1/idProduct").write_text("05b7\n")      # a different headset
    assert by_id()["hyperx"]["status"] == "na"


def test_tailscale_serve(host, monkeypatch):
    host["serve"] = {}
    c = by_id()["tailscale"]
    assert c["status"] == "problem" and c["fix"]["action"] == "tailscale-serve"
    monkeypatch.setattr(sc.shutil, "which", lambda name: None)
    assert by_id()["tailscale"]["status"] == "na"


def test_services_not_started_are_started_by_bragi_itself(host):
    host["active"]["bragi-peers.path"] = "inactive"
    c = by_id()["services"]
    assert c["status"] == "problem" and "bragi-peers.path" in c["detail"]
    sc.fix("services")
    assert host["started"] and "bragi-peers.path" in host["started"][0]


def test_root_fixes_need_the_helper_and_one_at_a_time(host):
    (host["dir"] / "linger/sage").unlink()
    host["dir"].joinpath("bragi-setup.path").unlink()
    with pytest.raises(sc.SetupError, match="no setup helper"):
        sc.fix("linger")
    host["dir"].joinpath("bragi-setup.path").write_text("")
    (host["dir"] / "state/setup.json").write_text(json.dumps({"state": "running", "action": "packages", "at": time.time()}))
    with pytest.raises(sc.SetupError, match="busy"):
        sc.fix("linger")
    (host["dir"] / "state/setup.json").write_text(json.dumps({"state": "done", "action": "packages", "at": time.time() - 5}))
    assert "Asked the setup helper" in sc.fix("linger")
    assert sc.pending()


def test_nothing_to_fix_and_unknown_checks(host):
    assert sc.fix("linger") == "Nothing to fix."
    with pytest.raises(sc.SetupError, match="Unknown check"):
        sc.fix("reboot-the-moon")


def test_the_settings_section(host, session):
    (host["dir"] / "linger/sage").unlink()
    client = TestClient(app)
    page = client.get("/setup").text
    assert "Runs without a login" in page and "Turn lingering on" in page
    page = client.post("/setup/fix/linger").text
    assert "Asked the setup helper" in page and "Working on" in page
    assert "Unknown check" in client.post("/setup/fix/nope").text
