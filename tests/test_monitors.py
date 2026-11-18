"""The KVM computers' monitors, reported by their tray apps (app/monitors.py)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import kvm_config, monitors, peer_control
from app.main import app


def monitor(**over):
    m = {"id": "MSI-3CD9-CD9M285200091", "name": "MAG 27CQ6F", "connector": "DP-2", "x": 0, "y": 0,
         "width": 1707, "height": 960, "pixel_width": 2560, "pixel_height": 1440, "scale": 1.5,
         "rotation": 0, "primary": True, "width_mm": 597, "height_mm": 336}
    m.update(over)
    return m


TV = monitor(id="GSM-0001-16843009", name="LG TV", connector="DP-3", x=1707, width=1422, height=800,
             pixel_width=1920, pixel_height=1080, scale=1.35, primary=False, width_mm=1600, height_mm=900)


def test_a_report_is_kept_and_only_a_change_counts(session):
    seen = []
    monitors.broadcast = seen.append
    assert monitors.report("sagedeck", {"type": "monitors", "source": "kscreen", "monitors": [monitor(), TV]}, now=100)
    assert not monitors.report("sagedeck", {"type": "monitors", "source": "kscreen", "monitors": [monitor(), TV]}, now=200)
    assert monitors.report("sagedeck", {"type": "monitors", "source": "kscreen", "monitors": [monitor()]}, now=300)
    assert seen == [{"type": "monitors"}, {"type": "monitors"}]


def test_a_mirrored_tv_has_no_area_but_is_known(session):
    tv_mirror = {k: TV[k] for k in ("id", "name", "connector", "pixel_width", "pixel_height", "width_mm", "height_mm")}
    monitors.report("sagedeck", {"source": "kscreen", "monitors": [monitor(mirrors=[tv_mirror])]}, now=100)
    view = monitors.view([{"name": "sagedeck"}], set())
    (msi,) = view["computers"][0]["monitors"]
    assert [r["name"] for r in msi["mirrors"]] == ["LG TV"]
    assert view["known"]["GSM-0001-16843009"] | {"seen": 0} == {
        "name": "LG TV", "width_mm": 1600, "height_mm": 900, "computer": "sagedeck",
        "mirror_of": "MSI-3CD9-CD9M285200091", "seen": 0}
    with pytest.raises(monitors.MonitorError):
        monitors.normalise({"monitors": [monitor(mirrors=[{"id": ""}])]})


def test_the_tv_is_known_by_its_edid_wherever_its_cable_is(session):
    monitors.report("sagedeck", {"source": "kscreen", "monitors": [monitor(), TV]}, now=100)
    monitors.report("sage", {"source": "windows", "monitors": [TV | {"connector": "DISPLAY2", "x": 2560}]}, now=200)
    known = monitors.view([], set())["known"]
    assert known["GSM-0001-16843009"]["computer"] == "sage"
    assert known["MSI-3CD9-CD9M285200091"]["computer"] == "sagedeck"


def test_reports_are_checked(session):
    for bad in ({"monitors": "x"}, {"monitors": [monitor(id="")]}, {"monitors": [monitor(width=0)]},
                {"monitors": [monitor(scale=100)]}, {"monitors": [monitor(x="1")]}, {"monitors": [monitor()] * 17}):
        with pytest.raises(monitors.MonitorError):
            monitors.normalise(bad)
    odd = monitors.normalise({"monitors": [monitor(rotation=45, width_mm=None, name="x" * 200)]})["monitors"][0]
    assert odd["rotation"] == 0 and odd["width_mm"] is None and len(odd["name"]) == 64


def test_the_setup_page_lists_each_computers_monitors(monkeypatch, session):
    monitors.report("sagedeck-cachyos", {"source": "kscreen", "monitors": [monitor(), TV]}, now=100)
    monkeypatch.setattr(kvm_config, "read", lambda: {"computers": [
        {"name": "sagedeck", "aliases": ["sagedeck-cachyos"]}, {"name": "sage", "aliases": []}]})
    monkeypatch.setattr(peer_control, "desktop_peers", lambda: {"sagedeck-cachyos"})

    view = TestClient(app).get("/kvm/monitors").json()
    deck, sage = view["computers"]
    assert (deck["computer"], deck["peer"], deck["online"], deck["source"]) == ("sagedeck", "sagedeck-cachyos", True, "kscreen")
    assert [m["name"] for m in deck["monitors"]] == ["MAG 27CQ6F", "LG TV"]
    assert (sage["peer"], sage["online"], sage["monitors"]) == (None, False, [])


def test_a_tray_apps_message_reaches_the_store(session):
    peer_control._desktop_message("sage", '{"type": "monitors", "source": "windows", "monitors": [%s]}'
                                  % __import__("json").dumps(monitor(connector="DISPLAY1")))
    peer_control._desktop_message("sage", '{"type": "monitors", "monitors": "nonsense"}')  # refused, logged
    assert monitors.view([{"name": "sage"}], set())["computers"][0]["monitors"][0]["connector"] == "DISPLAY1"
