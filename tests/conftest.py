"""Test fixtures: an isolated data/ directory and a fake PipeWire host.

Every test runs against `fake_pipewire.demo_session()` unless it builds
its own, and against a tmp_path data dir, so peers.yaml / balance.yaml /
viz_settings.yaml never touch the real ones.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audio_state, auth, bluetooth, eq_presets, gamepad, hardware, modules, headset_hid, layout, level_meter, macro_button, mic_monitor, monitors, peers as peers_module, pipewire, viz_settings, ws  # noqa: E402

from . import fake_pipewire  # noqa: E402


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Points DATA_DIR at a fresh tmp_path holding fake_pipewire.demo_peers(),
    the peers.yaml that matches the fake graph."""
    monkeypatch.setattr(peers_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(peers_module, "PEERS_FILE", tmp_path / "peers.yaml")
    monkeypatch.setattr(peers_module, "MANAGED_CONF_FILE", tmp_path / "peers.conf")
    monkeypatch.setattr(audio_state, "STATE_FILE", tmp_path / "balance.yaml")
    monkeypatch.setattr(viz_settings, "SETTINGS_FILE", tmp_path / "viz_settings.yaml")
    monkeypatch.setattr(layout, "LAYOUT_FILE", tmp_path / "layout.yaml")
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "auth.yaml")
    monkeypatch.setattr(headset_hid, "EQ_FILE", tmp_path / "headset_eq.yaml")
    monkeypatch.setattr(eq_presets, "FILE", tmp_path / "headset_eq_presets.yaml")
    monkeypatch.setattr(mic_monitor, "SETTINGS_FILE", tmp_path / "mic_monitor.yaml")
    monkeypatch.setattr(macro_button, "SETTINGS_FILE", tmp_path / "macro_button.yaml")
    monkeypatch.setattr(gamepad, "SETTINGS_FILE", tmp_path / "gamepad.yaml")
    monkeypatch.setattr(monitors, "SETTINGS_FILE", tmp_path / "monitors.yaml")
    monkeypatch.setattr(monitors, "broadcast", None)
    monkeypatch.setattr(monitors, "on_change", None)
    from app import hub_info
    monkeypatch.setattr(hub_info, "_cache", (0.0, {"type": "hub", "ws_url": "wss://hub.example.ts.net/ws/peer",
                                                   "tailscale_ip": "100.64.0.1", "lan_ip": "192.168.1.32",
                                                   "deskflow_hosts": []}))
    monkeypatch.setattr(gamepad, "hub", None)
    monkeypatch.setattr(bluetooth, "manager", None)
    modules.reset_cache()
    monkeypatch.setattr(hardware, "BATTERIES_FILE", tmp_path / "batteries.json")
    peers_module.save_peers(fake_pipewire.demo_peers())
    return tmp_path


@pytest.fixture(autouse=True)
def reset_ws_state():
    """ws.py keeps module-level throttle/ordering state keyed by control.
    Left over between tests it would silently suppress actions."""
    yield
    ws._last_applied.clear()
    ws._pending_value.clear()
    ws._worker_running.clear()
    ws._max_ts_seen.clear()
    ws._changed_nodes.clear()
    ws._drain_task = None
    level_meter._levels.clear()
    level_meter._stopped.clear()
    pipewire.forget_volumes()
    headset_hid._devices.clear()
    mic_monitor._runner = None
    macro_button._results.clear()
    macro_button._running.clear()
    macro_button._last_failure.clear()
    macro_button._written_seq.clear()


@pytest.fixture
def session(monkeypatch):
    fake = fake_pipewire.demo_session()
    monkeypatch.setattr(pipewire, "_run", fake.run)
    return fake


class RecordingManager:
    """Stands in for ws.manager, capturing broadcasts instead of queueing
    them onto real WebSockets."""

    def __init__(self, clients: bool = True):
        self.messages: list[dict] = []
        self._clients = clients
        self.wake = asyncio.Event()

    def broadcast_nowait(self, message: dict) -> None:
        self.messages.append(message)

    def broadcast_levels_nowait(self, message: dict) -> None:
        self.messages.append(message)

    def has_clients(self) -> bool:
        return self._clients

    def set_clients(self, present: bool) -> None:
        """What a browser tab connecting or closing does to the manager."""
        self._clients = present
        self.wake.set()

    def of_type(self, msg_type: str) -> list[dict]:
        return [m for m in self.messages if m.get("type") == msg_type]


@pytest.fixture
def broadcasts(monkeypatch):
    recorder = RecordingManager()
    monkeypatch.setattr(ws, "manager", recorder)
    return recorder
