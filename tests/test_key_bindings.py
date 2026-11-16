"""Key and button remapping in the KVM's input path (app/key_bindings.py)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import key_bindings
from app.main import app


@pytest.fixture
def kvm_dirs(tmp_path, monkeypatch):
    config, runtime = tmp_path / "config", tmp_path / "run"
    config.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("BRAGI_KVM_CONFIG_DIR", str(config))
    monkeypatch.setenv("BRAGI_KVM_DIR", str(runtime))
    (config / "bindings.json").write_text(json.dumps({"bindings": [
        {"device": "HyperX", "code": 163, "send": [29, 56, 66], "label": "Discord mute (Control+Alt+F8)"}],
        "remaps": [{"device": "MX Master 3S", "control": 195, "to": 86}]}))
    return config, runtime


def test_key_names_and_codes_go_both_ways():
    assert key_bindings.parse_combo("ctrl+Win+right") == [29, 125, 106]
    assert key_bindings.input_name(276) == "MouseForward"
    assert key_bindings.input_name(0x10C3) == "Thumb button"
    with pytest.raises(key_bindings.BindingError):
        key_bindings.parse_combo("Control+Hyper")


def test_bindings_are_read_named_and_saved_with_the_remaps_kept(kvm_dirs):
    config, _ = kvm_dirs
    view = key_bindings.read()
    assert view["bindings"][0]["input"] == "AudioNext" and view["bindings"][0]["sends"] == "Control+Alt+F8"

    key_bindings.save([{"device": "Logitech USB Receiver Mouse", "code": 275, "keys": "Control+Super+Left"}])
    saved = json.loads((config / "bindings.json").read_text())
    assert saved["bindings"] == [{"device": "Logitech USB Receiver Mouse", "code": 275, "send": [29, 125, 105],
                                  "label": "Control+Super+Left"}]
    assert saved["remaps"] == [{"device": "MX Master 3S", "control": 195, "to": 86}]

    for bad in ([{"code": 0, "keys": "A"}], [{"code": 275, "send": []}],
                [{"code": 275, "keys": "A"}, {"code": 275, "keys": "B"}], "nope"):
        with pytest.raises(key_bindings.BindingError):
            key_bindings.save(bad)


def test_identify_reports_the_last_press_through_the_dashboard(kvm_dirs, session):
    _, runtime = kvm_dirs
    client = TestClient(app)
    assert client.post("/kvm/bindings/identify", json={"on": True}).json()["identifying"] is True
    assert (runtime / "identify").exists()
    (runtime / "status.json").write_text(json.dumps({"lastInput": {"at": "t1", "device": "Lenovo TrackPoint Keyboard II", "code": 88}}))
    assert client.get("/kvm/bindings").json()["last_input"] == {
        "at": "t1", "device": "Lenovo TrackPoint Keyboard II", "code": 88, "input": "F12"}
    client.post("/kvm/bindings/identify", json={"on": False})
    assert not (runtime / "identify").exists()

    saved = client.post("/kvm/bindings", json={"bindings": [{"device": "", "code": 88, "keys": "Control+Alt+F12"}]})
    assert saved.status_code == 200 and saved.json()["bindings"][0]["sends"] == "Control+Alt+F12"
    assert client.post("/kvm/bindings", json={"bindings": [{"code": 88, "keys": "Nope"}]}).status_code == 400
