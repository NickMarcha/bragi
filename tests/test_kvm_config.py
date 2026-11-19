"""The KVM's settings, owned by Bragi (app/kvm_config.py). tests/data/kvm
holds sagepi's real server.ini and server.conf; the running server is
replaced by fakes for reload and restart."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import kvm_config
from app.main import app

DATA = Path(__file__).parent / "data" / "kvm"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / "deskflow-headless"
    d.mkdir()
    ini = (DATA / "server.ini").read_text().replace("/home/sage/.config/deskflow-headless/server.conf",
                                                   str(d / "server.conf").replace("\\", "/"))
    (d / "server.ini").write_text(ini)
    (d / "server.conf").write_text((DATA / "server.conf").read_text())
    (d / "webui-layout.json").write_text(json.dumps({"cells": {"sage": [1, 0], "sagedeck": [1, 1], "sage-dev": [3, 0]}}))
    (d / "env").write_text('DESKFLOW_EVDEV_HIDPP=\nDESKFLOW_EVDEV_REPEAT="500,30"\n')
    monkeypatch.setenv("BRAGI_KVM_CONFIG_DIR", str(d))
    state = {"reload": None, "restart": True, "reloads": 0, "restarts": 0}

    def reload():
        state["reloads"] += 1
        return state["reload"]

    def restart():
        state["restarts"] += 1
        return state["restart"]

    monkeypatch.setattr(kvm_config, "_reload", reload)
    monkeypatch.setattr(kvm_config, "_restart", restart)
    return d, state


def test_the_settings_read_as_the_box_has_them(cfg):
    config = kvm_config.read()
    assert config["host"] == "pi"
    assert config["computers"] == [{"name": "sage", "aliases": []},
                                   {"name": "sage-dev", "aliases": ["cachyos-x8664"]},
                                   {"name": "sagedeck", "aliases": ["sagedeck-cachyos-x8664"]}]
    assert config["cells"] == {"sage": [1, 0], "sagedeck": [1, 1], "sage-dev": [3, 0]}
    assert "keystroke(Control+Alt+F9) = switchToNextScreen" in config["hotkeys"]
    assert config["input"]["DESKFLOW_EVDEV_REPEAT"] == "500,30"


def test_saving_the_layout_links_neighbours_and_reloads_without_a_restart(cfg):
    d, state = cfg
    note = kvm_config.save_layout({"sagedeck": [0, 0], "sage": [1, 0], "sage-dev": [1, 1]},
                                  ["keystroke(Control+Alt+F9) = switchToNextScreen"])
    links, options, _ = kvm_config.parse_conf((d / "server.conf").read_text())
    assert links == {"sagedeck": {"right": "sage"}, "sage": {"left": "sagedeck", "down": "sage-dev"},
                     "sage-dev": {"up": "sage"}}
    assert options == ["keystroke(Control+Alt+F9) = switchToNextScreen"]
    assert (state["reloads"], state["restarts"]) == (1, 0) and "stayed connected" in note
    assert list((d / "backups").glob("server.conf.*"))


def test_a_refused_layout_puts_the_old_file_back(cfg):
    d, state = cfg
    before = (d / "server.conf").read_text()
    state["reload"] = "The KVM rejected the new settings"
    with pytest.raises(kvm_config.ConfigError, match="previous settings are back"):
        kvm_config.save_layout({"sage": [0, 0]}, ["keystroke(F1) = bogus("])
    assert (d / "server.conf").read_text() == before


@pytest.mark.parametrize("cells,hotkeys,match", [
    ({"nobody": [0, 0]}, [], "Unknown computer"),
    ({"sage": [0, 0], "sagedeck": [0, 0]}, [], "same place"),
    ({"sage": [40, 0]}, [], "Bad position"),
    ({}, ["end"], "Not allowed"),
    ({}, ["section: links"], "Not allowed"),
])
def test_bad_layouts_are_refused_before_anything_is_written(cfg, cells, hotkeys, match):
    d, state = cfg
    with pytest.raises(kvm_config.ConfigError, match=match):
        kvm_config.save_layout(cells, hotkeys)
    assert state["reloads"] == 0 and not (d / "backups").exists()


def test_removing_a_computer_takes_it_out_of_hotkeys_and_restarts(cfg):
    d, state = cfg
    note = kvm_config.save_computers([{"name": "sage"}, {"name": "sage-dev", "aliases": ["cachyos-x8664"]}])
    config = kvm_config.read()
    assert [c["name"] for c in config["computers"]] == ["sage", "sage-dev"]
    assert "keystroke(Control+Alt+F10) = switchToScreen(sagedeck)" not in config["hotkeys"]
    assert "keystroke(Control+Alt+F8) = keystroke(Control+Shift+Alt+M,sage:sage-dev)" in config["hotkeys"]
    assert "sagedeck" not in config["cells"]
    ini = (d / "server.ini").read_text()
    assert "computerName=pi" in ini and "externalConfigFile=" in ini and "[computer_pi]" in ini
    assert state["restarts"] == 1 and "restarted" in note


def test_a_computer_list_the_kvm_wont_start_with_is_undone(cfg):
    d, state = cfg
    before = (d / "server.ini").read_text()
    state["restart"] = False
    with pytest.raises(kvm_config.ConfigError, match="old ones are back"):
        kvm_config.save_computers([{"name": "sage"}])
    assert (d / "server.ini").read_text() == before


@pytest.mark.parametrize("computers,match", [
    ([{"name": "bad name"}], "Not a valid"),
    ([{"name": "sage"}, {"name": "sage"}], "used twice"),
    ([{"name": "pi"}], "used twice"),
    ([{"name": "a", "aliases": ["b c"]}], "Not a valid"),
])
def test_bad_computer_lists_are_refused(cfg, computers, match):
    with pytest.raises(kvm_config.ConfigError, match=match):
        kvm_config.save_computers(computers)


def test_input_options_keep_env_keys_bragi_doesnt_manage(cfg):
    d, state = cfg
    kvm_config.save_input({"DESKFLOW_EVDEV_REPEAT": "400,40", "DESKFLOW_EVDEV_ACCEL": "-0.2"})
    env = (d / "env").read_text()
    assert "DESKFLOW_EVDEV_HIDPP=" in env  # the fork's own page dropped this
    assert 'DESKFLOW_EVDEV_REPEAT="400,40"' in env and 'DESKFLOW_EVDEV_ACCEL="-0.2"' in env
    assert state["restarts"] == 1


@pytest.mark.parametrize("values,match", [
    ({"DESKFLOW_EVDEV_SHAPE": "big"}, "1920x1080"),
    ({"DESKFLOW_EVDEV_REPEAT": "fast"}, "500,30"),
    ({"DESKFLOW_EVDEV_ACCEL": "2"}, "between -1 and 1"),
    ({"XKB_DEFAULT_LAYOUT": "us$(reboot)"}, "aren't allowed"),
])
def test_bad_input_options_are_refused(cfg, values, match):
    with pytest.raises(kvm_config.ConfigError, match=match):
        kvm_config.save_input(values)


def test_the_real_server_conf_survives_a_round_trip():
    links, options, other = kvm_config.parse_conf((DATA / "server.conf").read_text())
    cells = kvm_config.derive_cells(links, ["sage", "sage-dev", "sagedeck"])
    again = kvm_config.parse_conf(kvm_config.build_conf(cells, options, other))
    assert again == (links, options, other)


def test_the_dashboard_routes(cfg, session):
    client = TestClient(app)
    assert client.get("/kvm/config").json()["host"] == "pi"
    ok = client.post("/kvm/config/layout", json={"cells": {"sage": [0, 0]}, "hotkeys": []}).json()
    assert ok["ok"] and ok["config"]["cells"] == {"sage": [0, 0]}
    refused = client.post("/kvm/config/computers", json={"computers": [{"name": "bad name"}]})
    assert refused.status_code == 400 and "Not a valid" in refused.json()["detail"]


MONITOR_LAYOUT = {"mode": "monitors", "groups": {
    "sage": {"x": 0, "y": 0, "scale": 1, "size": "os", "monitors": [
        {"id": "SAM", "name": "C34J79x", "x": 0, "y": 0, "width": 3440, "height": 1440},
        {"id": "BNQ", "name": "BenQ RL2455", "x": 3440, "y": 356, "width": 1920, "height": 1080}]},
    "sagedeck": {"x": 0, "y": 1440, "scale": 1, "size": "os", "monitors": [
        {"id": "MSI", "name": "MAG 27CQ6F", "x": 0, "y": 0, "width": 1707, "height": 960}]},
    "sage-dev": {"x": 3440, "y": 1440, "scale": 1, "size": "os", "monitors": [
        {"id": "BNQ2", "name": "BenQ GL2450H", "x": 0, "y": 0, "width": 1920, "height": 1080}]},
}}


def test_a_monitor_layout_writes_partial_edges(cfg):
    d, state = cfg
    kvm_config.save_layout({}, [], None, MONITOR_LAYOUT)
    conf = (d / "server.conf").read_text()
    assert "\t\tdown(0,31.85) = sagedeck\n\t\tdown(64.18,100) = sage-dev\n" in conf
    assert "\t\tup = sage(64.18,100)\n" in conf
    saved = json.loads((d / "webui-layout.json").read_text())
    assert saved["mode"] == "monitors" and set(saved["groups"]) == {"sage", "sagedeck", "sage-dev"}
    assert kvm_config.read()["layout"]["mode"] == "monitors"
    assert state["reloads"] == 1


def test_saving_hotkeys_alone_keeps_the_monitor_layout(cfg):
    d, _ = cfg
    kvm_config.save_layout({}, [], None, MONITOR_LAYOUT)
    kvm_config.save_layout({}, ["keystroke(Control+Alt+F9) = switchToNextScreen"])
    conf = (d / "server.conf").read_text()
    assert "down(0,31.85) = sagedeck" in conf and "switchToNextScreen" in conf


def test_removing_a_computer_drops_it_from_the_monitor_layout(cfg):
    d, _ = cfg
    kvm_config.save_layout({}, [], None, MONITOR_LAYOUT)
    kvm_config.save_computers([{"name": "sage"}, {"name": "sagedeck"}])
    conf = (d / "server.conf").read_text()
    assert "sage-dev" not in conf and "down(0,31.85) = sagedeck" in conf


def test_an_overlapping_monitor_layout_is_refused_before_anything_is_written(cfg):
    d, state = cfg
    before = (d / "server.conf").read_text()
    bad = json.loads(json.dumps(MONITOR_LAYOUT))
    bad["groups"]["sagedeck"]["y"] = 1000
    with pytest.raises(kvm_config.ConfigError, match="overlap"):
        kvm_config.save_layout({}, [], None, bad)
    assert (d / "server.conf").read_text() == before and state["reloads"] == 0


def test_the_layout_preview_route(cfg, session):
    client = TestClient(app)
    result = client.post("/kvm/config/layout/preview", json={"layout": MONITOR_LAYOUT}).json()
    assert result["links"]["sagedeck"] == ["up = sage(0,31.85)"] and result["errors"] == []
    refused = client.post("/kvm/config/layout/preview", json={"layout": {"mode": "monitors", "groups": {"x": {}}}})
    assert refused.status_code == 400 and "Unknown computer" in refused.json()["detail"]

# --- layouts per setup ---------------------------------------------------------------

def report(peer, *monitors_):
    from app import monitors
    monitors.report(peer, {"source": "test", "monitors": [
        {"id": i, "name": n, "connector": "X", "x": x, "y": y, "width": w, "height": h, "pixel_width": w,
         "pixel_height": h, "scale": 1, "rotation": 0, "primary": False, "width_mm": None, "height_mm": None,
         "mirrors": []} for i, n, x, y, w, h in monitors_]})


def desk():
    report("sage", ("SAM", "C34J79x", 0, 0, 3440, 1440), ("BNQ", "BenQ RL2455", 3440, 356, 1920, 1080))
    report("sage-dev", ("BNQ2", "BenQ GL2450H", 0, 0, 1920, 1080))


def test_saving_a_monitor_layout_keeps_it_for_the_setup(cfg):
    d, _ = cfg
    desk()
    report("sagedeck", ("MSI", "MAG 27CQ6F", 0, 0, 1707, 960))
    kvm_config.save_layout({}, [], None, MONITOR_LAYOUT)
    saved = json.loads((d / "webui-layout.json").read_text())
    key = "sage=BNQ,SAM|sage-dev=BNQ2|sagedeck=MSI"
    assert saved["active"] == key
    assert saved["setups"][key]["label"] == "sage: C34J79x + BenQ RL2455; sage-dev: BenQ GL2450H; sagedeck: MAG 27CQ6F"
    layout = kvm_config.read()["layout"]
    assert layout["setup"]["known"] and layout["setups"][0]["active"] and layout["setups"][0]["current"]


def undocked_layout():
    layout = json.loads(json.dumps(MONITOR_LAYOUT))
    # Undocked, sagedeck's own screen sits to the left of sage.
    layout["groups"]["sagedeck"] = {"x": -1280, "y": 0, "scale": 1, "size": "os", "monitors": [
        {"id": "LEN", "name": "Go Display", "x": 0, "y": 0, "width": 1280, "height": 800}]}
    return layout


def test_the_kvm_follows_the_setup_back_and_forth(cfg):
    d, state = cfg
    desk()
    report("sagedeck", ("MSI", "MAG 27CQ6F", 0, 0, 1707, 960))
    kvm_config.save_layout({}, [], None, MONITOR_LAYOUT)

    report("sagedeck", ("LEN", "Go Display", 0, 0, 1280, 800))   # undocked: a setup with no layout yet
    assert kvm_config.follow_setup() is None
    assert "down(0,31.85) = sagedeck" in (d / "server.conf").read_text()   # the old layout stays
    view = kvm_config.read()["layout"]
    assert not view["setup"]["known"] and view["groups"]["sagedeck"]["monitors"][0]["id"] == "MSI"  # closest saved
    kvm_config.save_layout({}, [], None, undocked_layout())
    assert "left(0,55.56) = sagedeck" in (d / "server.conf").read_text()

    report("sagedeck", ("MSI", "MAG 27CQ6F", 0, 0, 1707, 960))   # docked again
    reloads = state["reloads"]
    note = kvm_config.follow_setup()
    assert note and "MAG 27CQ6F" in note
    conf = (d / "server.conf").read_text()
    assert "down(0,31.85) = sagedeck" in conf and "left(0,55.56)" not in conf
    assert state["reloads"] == reloads + 1
    assert kvm_config.follow_setup() is None   # already in use


def test_following_draws_the_saved_layout_with_the_monitors_connected_now(cfg):
    d, _ = cfg
    desk()
    report("sagedeck", ("MSI", "MAG 27CQ6F", 0, 0, 1707, 960))
    kvm_config.save_layout({}, [], None, MONITOR_LAYOUT)
    report("sagedeck", ("LEN", "Go Display", 0, 0, 1280, 800))
    kvm_config.save_layout({}, [], None, undocked_layout())
    # Docked again, but now at 1920 wide (the scaling changed): same monitor, same setup.
    report("sagedeck", ("MSI", "MAG 27CQ6F", 0, 0, 1920, 1080))
    kvm_config.follow_setup()
    assert "down(0,35.82) = sagedeck" in (d / "server.conf").read_text()


def test_the_grid_doesnt_follow_setups(cfg):
    desk()
    report("sagedeck", ("MSI", "MAG 27CQ6F", 0, 0, 1707, 960))
    kvm_config.save_layout({}, [], None, MONITOR_LAYOUT)
    report("sagedeck", ("LEN", "Go Display", 0, 0, 1280, 800))
    kvm_config.save_layout({}, [], None, undocked_layout())
    kvm_config.save_layout({"sage": [0, 0]}, [], None, {"mode": "grid"})
    report("sagedeck", ("MSI", "MAG 27CQ6F", 0, 0, 1707, 960))
    assert kvm_config.follow_setup() is None


def test_a_saved_layout_can_be_used_by_hand_and_forgotten(cfg):
    d, _ = cfg
    desk()
    report("sagedeck", ("MSI", "MAG 27CQ6F", 0, 0, 1707, 960))
    kvm_config.save_layout({}, [], None, MONITOR_LAYOUT)
    docked = kvm_config.read()["layout"]["setup"]["key"]
    report("sagedeck", ("LEN", "Go Display", 0, 0, 1280, 800))
    kvm_config.save_layout({}, [], None, undocked_layout())
    assert "Using the layout" in kvm_config.use_setup(docked)
    assert "down(0,31.85) = sagedeck" not in (d / "server.conf").read_text()  # drawn with the Go Display
    assert json.loads((d / "webui-layout.json").read_text())["active"] == docked
    assert "Forgot" in kvm_config.delete_setup(docked)
    assert [s["key"] for s in kvm_config.read()["layout"]["setups"]] == ["sage=BNQ,SAM|sage-dev=BNQ2|sagedeck=LEN"]
    with pytest.raises(kvm_config.ConfigError, match="no saved layout"):
        kvm_config.use_setup(docked)


def test_kvm_follow_waits_for_the_monitors_to_settle(monkeypatch):
    import asyncio
    from app import kvm_follow
    calls, sent = [], []
    monkeypatch.setattr(kvm_follow, "SETTLE", 0.05)
    monkeypatch.setattr(kvm_follow.kvm_config, "follow_setup", lambda: calls.append(1) or "switched")

    async def run():
        kvm_follow.start(asyncio.get_running_loop(), sent.append)
        for _ in range(3):
            kvm_follow.changed()
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert calls == [1] and sent == [{"type": "kvm_layout", "note": "switched"}]