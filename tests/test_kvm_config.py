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
