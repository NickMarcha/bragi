"""Bragi's KVM actions for buttons (app/actions.py), and the macro button
using them (app/macro_button.py)."""

from __future__ import annotations

import asyncio

import pytest

from app import actions, kvm, macro_button


def test_kvm_actions_are_checked_before_they_are_saved():
    assert actions.valid_kvm({"type": "kvm_next", "extra": 1}) == {"type": "kvm_next"}
    assert actions.valid_kvm({"type": "kvm_switch", "computer": " sage "}) == {"type": "kvm_switch", "computer": "sage"}
    assert actions.valid_kvm({"type": "kvm_keys", "keys": "Control+Shift+Alt+M",
                              "computers": ["sage", "sage", "sagedeck"]}) == \
        {"type": "kvm_keys", "keys": "Control+Shift+Alt+M", "computers": ["sage", "sagedeck"]}
    for bad in ({"type": "kvm_switch", "computer": ""}, {"type": "kvm_switch", "computer": "a:b"},
                {"type": "kvm_keys", "keys": "Control,M"}, {"type": "kvm_keys", "keys": "M", "computers": "sage"}):
        with pytest.raises(ValueError):
            actions.valid_kvm(bad)


def test_next_computer_follows_the_setup_order_and_skips_ones_not_connected():
    order = ["sage", "sage-dev", "sagedeck"]
    assert actions.next_computer(order, ["sage", "sagedeck"], "sage") == "sagedeck"
    assert actions.next_computer(order, ["sage", "sagedeck"], "sagedeck") == "sage"
    assert actions.next_computer(order, ["sage", "laptop"], "sage") == "laptop"
    assert actions.next_computer(order, ["sagedeck"], None) == "sagedeck"
    assert actions.next_computer(order, [], "sage") is None


def test_kvm_actions_run_through_the_kvm(monkeypatch):
    done = []
    monkeypatch.setattr(kvm, "read_view", lambda: {"available": True, "active": "sage",
                                                   "computers": ["sage", "sagedeck"]})
    monkeypatch.setattr(kvm, "switch", lambda c: done.append(("switch", c)))
    monkeypatch.setattr(kvm, "keystroke", lambda k, c: done.append(("keys", k, c)))
    monkeypatch.setattr(actions, "computers", lambda: ["sage", "sage-dev", "sagedeck"])

    assert asyncio.run(actions.run_kvm({"type": "kvm_next"})) == (True, "switched to sagedeck")
    assert asyncio.run(actions.run_kvm({"type": "kvm_keys", "keys": "Control+M", "computers": []})) == \
        (True, "pressed Control+M on the active computer")

    def refuse(_):
        raise kvm.KvmError("ghost isn't connected.")

    monkeypatch.setattr(kvm, "switch", refuse)
    assert asyncio.run(actions.run_kvm({"type": "kvm_switch", "computer": "ghost"})) == (False, "ghost isn't connected.")
    assert done == [("switch", "sagedeck"), ("keys", "Control+M", None)]


def test_the_old_web_ui_keystroke_request_reads_as_a_kvm_action():
    """sagepi's double press was saved as an HTTP request to the fork's web
    UI socket; that page is going away."""
    old = {"type": "http", "method": "POST", "target": "socket", "url": "",
           "socket": "/run/user/1000/deskflow-headless/webui.sock", "path": "/api/keystroke",
           "headers": {"X-Deskflow-Webui": "1"},
           "body": '{"keys": "Control+Shift+Alt+M", "computers": ["sage", "sage-dev", "sagedeck"]}'}
    assert actions.migrate_webui_keystroke(old) == {"type": "kvm_keys", "keys": "Control+Shift+Alt+M",
                                                    "computers": ["sage", "sage-dev", "sagedeck"]}
    other = {**old, "path": "/api/switch"}
    assert actions.migrate_webui_keystroke(other) is other


def test_the_macro_button_saves_and_runs_kvm_actions(monkeypatch, tmp_path):
    monkeypatch.setattr(macro_button, "SETTINGS_FILE", tmp_path / "macro_button.yaml")
    ran = []

    async def fake_run(action):
        ran.append(action)
        return True, "switched to sagedeck"

    monkeypatch.setattr(actions, "run_kvm", fake_run)
    saved = asyncio.run(macro_button.set_action("SERIAL", "single", {"type": "kvm_next"}))
    assert saved == {"type": "kvm_next"}
    result = asyncio.run(macro_button.press("SERIAL", "single"))
    assert result["ok"] and result["text"] == "switched to sagedeck" and ran == [{"type": "kvm_next"}]


def test_keys_go_through_a_tray_app_that_can_press_them_and_the_kvm_for_the_rest(monkeypatch, session):
    """Wayland clients drop KVM keys while the cursor is elsewhere
    (deskflow#10393); a tray app that said "keys" presses them itself."""
    from fastapi.testclient import TestClient

    from app import kvm_config
    from app.main import app
    monkeypatch.setattr(kvm_config, "read", lambda: {"computers": [
        {"name": "sage", "aliases": []}, {"name": "sage-dev", "aliases": []}, {"name": "sagedeck", "aliases": ["deck"]}]})
    sent_to_kvm = []
    monkeypatch.setattr(kvm, "keystroke", lambda keys, computers: sent_to_kvm.append((keys, computers)))
    client = TestClient(app)
    with client.websocket_connect("/ws/peer/deck") as deck, client.websocket_connect("/ws/peer/sage") as sage:
        deck.receive_json(), sage.receive_json()  # the "kvm" view
        deck.send_json({"type": "client_hello", "features": ["keys"]})
        sage.send_json({"type": "client_hello", "features": []})
        # The hellos land before the next message is handled; each is answered
        # with where the hub is (bragi#61).
        deck.send_json({"type": "kvm_switch", "computer": "ghost"})
        assert deck.receive_json()["type"] == "hub"
        deck.receive_json()
        ok, text = asyncio.run(actions.run_kvm({"type": "kvm_keys", "keys": "Control+Shift+Alt+M",
                                                "computers": ["sage", "sage-dev", "sagedeck"]}))
        assert deck.receive_json() == {"type": "press_keys", "codes": [29, 42, 56, 50]}
    assert ok and text == "pressed Control+Shift+Alt+M on deck (Bragi app), sage, sage-dev"
    assert sent_to_kvm == [("Control+Shift+Alt+M", ["sage", "sage-dev"])]


def test_keys_the_tray_apps_dont_know_all_go_through_the_kvm(monkeypatch):
    monkeypatch.setattr(actions.peer_control, "peer_for_computer", lambda computer, setup: computer)
    monkeypatch.setattr(actions.peer_control, "has_feature", lambda peer, feature: True)
    assert actions.split_by_tray_app("Control+Hyper_L", ["sagedeck"]) == ([], ["sagedeck"])
    assert actions.split_by_tray_app("Control+M", ["sagedeck"]) == ([("sagedeck", [29, 50])], [])
