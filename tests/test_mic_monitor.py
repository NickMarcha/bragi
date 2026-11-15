"""Software mic monitoring (app/mic_monitor.py): a pw-loopback from the
headset's own mic to its own speaker, switched from the headset dialog.

pw-loopback is replaced at mic_monitor._spawn by FakeLoopback, which puts
the two stream nodes a real one creates into the fake graph and takes them
out again when it's stopped, the way PipeWire drops a client's nodes when
the process goes.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from app import headset_hid, mic_monitor, views, ws
from app.main import app

from .fake_hyperx import SERIAL, FakeDongle
from .fake_pipewire import HEADSET_CARD_ID, OFF_PROFILE, SECOND_CARD_ID, STEREO_PROFILE, FakeNode

HEADSET_SINK = 233
HEADSET_MIC = 124
SOURCE_NAME = f"alsa_input.{HEADSET_CARD_ID}.mono-fallback"
SINK_NAME = f"alsa_output.{HEADSET_CARD_ID}.analog-stereo"
CAPTURE_NODE, PLAYBACK_NODE = mic_monitor.node_names(HEADSET_CARD_ID)


class FakeLoopback:
    def __init__(self, session, args, node_id):
        self.args = args
        self.returncode = None
        self.session = session
        self.nodes = [FakeNode(node_id, CAPTURE_NODE, CAPTURE_NODE, "Stream/Input/Audio"),
                      FakeNode(node_id + 1, PLAYBACK_NODE, "Bragi mic monitor", "Stream/Output/Audio")]
        session.free_nodes.extend(self.nodes)

    def exit(self, code: int = 1) -> None:
        self.returncode = code
        for node in self.nodes:
            self.session.free_nodes.remove(node)

    def terminate(self) -> None:
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def loopbacks(session, monkeypatch):
    started: list[FakeLoopback] = []

    async def spawn(args):
        started.append(FakeLoopback(session, args, 700 + 10 * len(started)))
        return started[-1]

    monkeypatch.setattr(mic_monitor, "_spawn", spawn)
    return started


@pytest.fixture
def dongle_attached():
    dongle = FakeDongle()
    hs = headset_hid.attach(SERIAL, dongle.write, lambda serial: None)
    dongle.deliver = hs.handle_report
    hs.refresh()
    return hs


def running(loopbacks):
    return [lb for lb in loopbacks if lb.returncode is None]


async def settle():
    """Two passes: one starts the loopback, the next finds its playback
    node and puts the saved volume on it, as supervise() does."""
    runner = mic_monitor.runner()
    await runner.reconcile()
    await runner.reconcile()


def test_it_is_off_by_default(session, dongle_attached):
    assert views.headset_controls_view(HEADSET_CARD_ID)["monitor"] == {"enabled": False, "volume": 1.0}


async def test_nothing_on_costs_no_pw_dump(session, loopbacks):
    await mic_monitor.runner().reconcile()

    assert loopbacks == []
    assert session.count("pw-dump") == 0


async def test_turning_it_on_loops_the_headsets_own_mic_into_its_own_speaker(session, loopbacks, broadcasts,
                                                                              dongle_attached):
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_monitor", "value": True})
    await settle()

    (lb,) = running(loopbacks)
    capture = next(a for a in lb.args if a.startswith("--capture-props="))
    playback = next(a for a in lb.args if a.startswith("--playback-props="))
    assert lb.args[0] == "pw-loopback"
    assert f"target.object={SOURCE_NAME} " in capture and f"target.object={SINK_NAME} " in playback
    for props in (capture, playback):
        assert "node.latency=256/48000" in props
        # Never onto the default sink (which can be a peer's) while the headset is gone.
        assert "node.dont-fallback=true" in props
        assert "node.linger" not in props
    message = broadcasts.of_type("headset_controls")[-1]
    assert message["key"] == HEADSET_CARD_ID and message["controls"]["monitor"]["enabled"] is True


async def test_the_volume_goes_on_the_loopback_only(session, loopbacks, broadcasts, dongle_attached):
    before = {n.id: n.volume for n in session.live_nodes()}
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_monitor_volume", "value": 0.4})
    await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_monitor", "value": True})
    await settle()

    (lb,) = running(loopbacks)
    playback, capture = lb.nodes[1], lb.nodes[0]
    assert playback.volume == pytest.approx(0.4)
    assert capture.volume == 1.0
    # Not the headset's mic (what the peers hear), not its speaker, not any peer.
    assert {n.id: n.volume for n in session.live_nodes() if n.id in before} == before


async def test_dragging_the_slider_costs_no_pw_dump(session, loopbacks, broadcasts, dongle_attached):
    await mic_monitor.set_enabled(HEADSET_CARD_ID, True)
    await settle()
    session.reset_calls()

    for value in (0.9, 0.7, 0.5):
        await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": "set_monitor_volume",
                               "value": value})

    assert running(loopbacks)[0].nodes[1].volume == pytest.approx(0.5)
    assert session.count("pw-dump") == 0
    assert session.count("wpctl", "set-volume") == 3


async def test_both_settings_are_saved(session, loopbacks, dongle_attached, data_dir):
    await mic_monitor.set_enabled(HEADSET_CARD_ID, True)
    await mic_monitor.set_volume(HEADSET_CARD_ID, 0.6)

    saved = yaml.safe_load((data_dir / "mic_monitor.yaml").read_text())
    assert saved == {HEADSET_CARD_ID: {"enabled": True, "volume": 0.6}}


async def test_a_restart_brings_it_back_at_the_saved_volume(session, loopbacks, data_dir):
    (data_dir / "mic_monitor.yaml").write_text(yaml.safe_dump({HEADSET_CARD_ID: {"enabled": True, "volume": 0.3}}))

    await settle()

    (lb,) = running(loopbacks)
    assert lb.nodes[1].volume == pytest.approx(0.3)


async def test_turning_it_off_stops_the_loopback(session, loopbacks, dongle_attached):
    await mic_monitor.set_enabled(HEADSET_CARD_ID, True)
    await settle()

    await mic_monitor.set_enabled(HEADSET_CARD_ID, False)
    await mic_monitor.runner().reconcile()

    assert running(loopbacks) == []
    assert not any(n.name.startswith(mic_monitor.NODE_PREFIX) for n in session.live_nodes())


async def test_an_unplug_stops_it_and_a_replug_starts_it_again(session, loopbacks):
    await mic_monitor.set_enabled(HEADSET_CARD_ID, True)
    await settle()
    card = session.cards[0]

    card.active_index = OFF_PROFILE  # the headset's nodes are gone, as when unplugged
    await mic_monitor.runner().reconcile()
    assert running(loopbacks) == []

    card.active_index = STEREO_PROFILE
    await settle()
    assert len(loopbacks) == 2 and len(running(loopbacks)) == 1


async def test_a_replug_between_looks_still_restarts_it(session, loopbacks):
    """Same node names, new ids: the old loopback's links went with the
    old nodes."""
    await mic_monitor.set_enabled(HEADSET_CARD_ID, True)
    await settle()

    for node in session.cards[0].nodes:
        node.id += 1000
    await settle()

    assert len(loopbacks) == 2 and len(running(loopbacks)) == 1


async def test_a_loopback_that_exits_is_started_again(session, loopbacks):
    await mic_monitor.set_enabled(HEADSET_CARD_ID, True)
    await settle()

    loopbacks[0].exit(1)
    await settle()

    assert len(loopbacks) == 2 and running(loopbacks) == [loopbacks[1]]
    assert loopbacks[1].nodes[1].volume == pytest.approx(1.0)


async def test_stopping_bragi_stops_every_loopback(session, loopbacks):
    await mic_monitor.set_enabled(HEADSET_CARD_ID, True)
    await settle()

    await mic_monitor.runner().stop_all()

    assert running(loopbacks) == []


def test_a_leftover_loopback_from_an_earlier_run_is_removed(session):
    session.free_nodes += [
        FakeNode(800, CAPTURE_NODE, CAPTURE_NODE, "Stream/Input/Audio", props={"client.id": 77}),
        FakeNode(801, PLAYBACK_NODE, PLAYBACK_NODE, "Stream/Output/Audio", props={"client.id": 77}),
        FakeNode(802, "mic-to-deck-capture", "mic-to-deck-capture", "Stream/Input/Audio", props={"client.id": 40}),
    ]

    assert mic_monitor.sweep_strays() == [77]
    assert [c for c in session.calls if c[0] == "pw-cli"] == [["pw-cli", "destroy", "77"]]


async def test_bad_values_change_nothing(session, loopbacks, broadcasts, dongle_attached, data_dir):
    for action, value in (("set_monitor", "yes"), ("set_monitor_volume", 2), ("set_monitor_volume", True),
                          ("set_monitor_volume", "loud")):
        await ws.apply_action({"target": "headset", "key": HEADSET_CARD_ID, "action": action, "value": value})

    assert not (data_dir / "mic_monitor.yaml").exists()
    assert broadcasts.of_type("headset_controls") == []


async def test_a_headset_without_the_dialog_ignores_the_actions(session, loopbacks, broadcasts, data_dir):
    await ws.apply_action({"target": "headset", "key": SECOND_CARD_ID, "action": "set_monitor", "value": True})

    assert not (data_dir / "mic_monitor.yaml").exists()


def test_the_dialog_tells_the_two_monitors_apart(session, dongle_attached, data_dir):
    (data_dir / "mic_monitor.yaml").write_text(yaml.safe_dump({HEADSET_CARD_ID: {"enabled": True, "volume": 0.45}}))

    html = TestClient(app).get("/").text

    assert "Mic monitoring (hear your own voice)" in html
    assert "Headset&#39;s built-in monitoring (may be noisy)" in html or "Headset's built-in monitoring (may be noisy)" in html
    assert 'data-monitor="enabled" checked' in html
    assert 'data-monitor="volume" min="0" max="150" step="1" value="45"' in html
