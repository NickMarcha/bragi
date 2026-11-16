"""Keyboard hotkeys that run Bragi actions (app/hotkey_actions.py): saved
with the KVM layout, heard from the KVM server's IPC socket."""

from __future__ import annotations

import asyncio
import socket

import pytest

from app import actions, hotkey_actions, kvm, kvm_config
from tests.test_kvm_config import cfg  # noqa: F401 (fixture)

LINE = "keystroke(Control+Alt+F8) = notify(bragi-1)"
MUTE = {"type": "kvm_keys", "keys": "Control+Shift+Alt+M", "computers": ["sage", "sage-dev", "sagedeck"]}


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(hotkey_actions, "SETTINGS_FILE", tmp_path / "hotkey_actions.yaml")
    return tmp_path / "hotkey_actions.yaml"


def test_a_bragi_hotkey_is_saved_with_its_action(cfg, saved):  # noqa: F811
    d, state = cfg
    kvm_config.save_layout({"sage": [1, 0]}, [LINE, "keystroke(Control+Alt+F9) = switchToNextScreen"],
                           {"bragi-1": MUTE, "bragi-7": {"type": "kvm_next"}})
    assert LINE in (d / "server.conf").read_text()
    assert hotkey_actions.load() == {"bragi-1": MUTE}  # bragi-7 isn't used by any line
    assert kvm_config.read()["hotkey_actions"] == {"bragi-1": MUTE}

    # Saved again without actions (the API's layout call): the saved ones stay.
    kvm_config.save_layout({"sage": [1, 0]}, [LINE])
    assert hotkey_actions.load() == {"bragi-1": MUTE}


def test_a_bragi_hotkey_without_a_good_action_is_refused_before_anything_is_written(cfg, saved):  # noqa: F811
    d, _ = cfg
    before = (d / "server.conf").read_text()
    for actions_wanted in ({}, {"bragi-1": {"type": "kvm_keys", "keys": "a,b"}}):
        with pytest.raises(kvm_config.ConfigError):
            kvm_config.save_layout({"sage": [1, 0]}, [LINE], actions_wanted)
    assert (d / "server.conf").read_text() == before and not saved.exists()


def test_a_refused_reload_doesnt_save_the_actions(cfg, saved):  # noqa: F811
    _, state = cfg
    state["reload"] = "The KVM rejected the new settings"
    with pytest.raises(kvm_config.ConfigError):
        kvm_config.save_layout({"sage": [1, 0]}, [LINE], {"bragi-1": MUTE})
    assert not saved.exists()


def test_a_notified_hotkey_runs_its_action(saved, monkeypatch):
    hotkey_actions.save({"bragi-1": MUTE})
    ran = []

    async def fake_run(action):
        ran.append(action)
        return True, "pressed"

    monkeypatch.setattr(actions, "run_kvm", fake_run)
    asyncio.run(hotkey_actions.run("bragi-1"))
    asyncio.run(hotkey_actions.run("bragi-9"))  # nothing saved for it
    assert ran == [MUTE] and hotkey_actions.results()["bragi-1"]["ok"]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")
def test_the_listener_hears_action_lines_on_the_core_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAGI_KVM_DIR", str(tmp_path))
    heard = []

    async def on_action(name):
        heard.append(name)

    async def run():
        async def client(reader, writer):
            writer.write(b"hello=1.27\naction=bragi-1\nstatus=x\naction=bragi-2\n")
            await writer.drain()

        server = await asyncio.start_unix_server(client, str(tmp_path / "core.sock"))
        task = asyncio.create_task(kvm.listen_actions(on_action))
        for _ in range(50):
            if len(heard) == 2:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        server.close()

    asyncio.run(run())
    assert heard == ["bragi-1", "bragi-2"]
