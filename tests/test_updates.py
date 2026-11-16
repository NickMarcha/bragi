"""Updates for a packaged Bragi (app/updates.py and packaging/bragi-update):
Bragi asks the root updater through a request file and shows what it
wrote back."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import updates
from app.main import app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "STATE_DIR", tmp_path)
    monkeypatch.setattr(updates, "VERSION_FILE", tmp_path / "VERSION")
    (tmp_path / "update-request").write_text("")
    (tmp_path / "VERSION").write_text("0.1.0\n")
    return tmp_path


def write_status(state_dir, **fields):
    data = {"state": "done", "action": "check", "message": "checked", "at": 1763251200,
            "packages": {"bragi": {"installed": "0.1.0", "candidate": "0.1.0"},
                         "deskflow": {"installed": "1.27.0.26", "candidate": "1.27.0.26"}}}
    data.update(fields)
    (state_dir / "update.json").write_text(json.dumps(data))


def test_outside_a_package_there_is_nothing_to_update(tmp_path, monkeypatch, session):
    monkeypatch.setattr(updates, "STATE_DIR", tmp_path / "missing")
    assert updates.status()["available"] is False
    with pytest.raises(updates.UpdateError):
        updates.request("check")
    assert "wasn't installed as a package" in TestClient(app).get("/updates").text


def test_up_to_date_shows_the_versions_and_a_check_button(state, session):
    write_status(state)
    page = TestClient(app).get("/updates").text
    assert "Bragi 0.1.0, Deskflow 1.27.0.26" in page and "Up to date" in page
    assert "Install update" not in page


def test_a_newer_package_offers_install_which_asks_the_updater(state, session):
    write_status(state, packages={"bragi": {"installed": "0.1.0", "candidate": "0.2.0"}})
    client = TestClient(app)
    page = client.get("/updates").text
    assert "bragi 0.2.0" in page and "Install update" in page

    client.post("/updates/upgrade")
    assert (state / "update-request").read_text() == "upgrade\n"


def test_while_the_updater_runs_nothing_else_is_asked(state, session):
    write_status(state, state="running", action="upgrade")
    client = TestClient(app)
    page = client.post("/updates/check").text
    assert "already running" in page and "Installing the update" in page and "<button" not in page
    assert (state / "update-request").read_text() == ""


def test_unknown_actions_are_refused(state, session):
    assert "Unknown update action" in TestClient(app).post("/updates/remove").text


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="runs the updater script with bash")
def test_the_updater_script_writes_its_status(tmp_path):
    """Runs packaging/bragi-update against fake apt and dpkg-query."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for name, body in {
        "apt-get": f'echo "apt-get $*" >> {calls}',
        "apt-cache": 'echo "  Candidate: 0.2.0"',
        "dpkg-query": 'echo -n 0.1.0',
    }.items():
        (bin_dir / name).write_text("#!/bin/bash\n" + body + "\n")
        (bin_dir / name).chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "update-request").write_text("upgrade\n")
    script = (ROOT / "packaging" / "bragi-update").read_text().replace("STATE=/var/lib/bragi", f"STATE={state}")
    (tmp_path / "bragi-update").write_text(script)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    subprocess.run(["bash", str(tmp_path / "bragi-update")], check=True, env=env)

    status = json.loads((state / "update.json").read_text())
    assert (status["state"], status["action"]) == ("done", "upgrade")
    assert status["packages"]["bragi"] == {"installed": "0.1.0", "candidate": "0.2.0"}
    log = calls.read_text()
    assert "apt-get update" in log and "--only-upgrade" in log and "bragi deskflow" in log

    # The timer's unit passes "check" and never installs, whatever the last request was.
    calls.write_text("")
    subprocess.run(["bash", str(tmp_path / "bragi-update"), "check"], check=True, env=env)
    assert "--only-upgrade" not in calls.read_text()

    # A request written while it runs is picked up when it's done.
    (state / "update-request").write_text("check\n")
    (bin_dir / "apt-get").write_text(
        "#!/bin/bash\n"
        f'echo "apt-get $*" >> {calls}\n'
        f'if [[ "$*" == update* && ! -e {tmp_path}/asked ]]; then touch {tmp_path}/asked; sleep 0.05; echo upgrade > {state}/update-request; fi\n')
    calls.write_text("")
    subprocess.run(["bash", str(tmp_path / "bragi-update")], check=True, env=env)
    log = calls.read_text()
    assert log.count("apt-get update") == 2 and "--only-upgrade" in log


def test_a_request_shows_as_queued_until_the_updater_answers(state, session):
    """The path unit takes a moment to start the updater: the page says it's
    on from the click on, and a second click is refused."""
    write_status(state, packages={"bragi": {"installed": "0.1.0", "candidate": "0.2.0"}})
    client = TestClient(app)
    page = client.post("/updates/upgrade").text
    assert "Installing the update" in page
    assert "already running" in client.post("/updates/check").text

    write_status(state, at=int(time.time()) + 5, message="upgraded", action="upgrade")
    assert "Installing the update" not in client.get("/updates").text


def test_a_request_nobody_picks_up_says_so(state, session):
    write_status(state)
    (state / "update-request").write_text("check\n")
    old = time.time() - updates.QUEUE_TIMEOUT - 5
    os.utime(state / "update-request", (old, old))
    page = TestClient(app).get("/updates").text
    assert "The updater didn" in page and "Check again" in page


def test_a_polling_page_from_an_older_version_reloads_once_a_new_one_runs(state, session):
    """0.3.1 and 0.3.2 pages poll /updates?since=; they still reload."""
    write_status(state, state="running", action="upgrade")
    client = TestClient(app)
    (state / "VERSION").write_text("0.2.0\n")
    assert "HX-Refresh" not in client.get("/updates?since=0.1.0").headers

    write_status(state, action="upgrade", message="upgraded", at=int(time.time()) + 5)
    assert client.get("/updates?since=0.1.0").headers.get("HX-Refresh") == "true"
    assert "HX-Refresh" not in client.get("/updates?since=0.2.0").headers


def test_one_action_at_a_time(state, session):
    """At most one button, and none while the updater runs: a click can't be
    repeated while the first is still being handled."""
    client = TestClient(app)
    write_status(state)
    page = client.get("/updates").text
    assert page.count("<button") == 1 and "Check for updates" in page and 'hx-disabled-elt="this"' in page

    write_status(state, packages={"bragi": {"installed": "0.1.0", "candidate": "0.2.0"}})
    page = client.get("/updates").text
    assert page.count("<button") == 1 and "Install update" in page and "Check for updates" not in page

    page = client.post("/updates/upgrade").text
    assert "<button" not in page and "Installing the update" in page


def test_dashboards_hear_when_the_section_changes(state, session):
    """No polling: updates.watch broadcasts "updates" when the section would
    look different, and ws.js reloads it."""
    import asyncio
    write_status(state)
    heard = []

    async def run():
        task = asyncio.create_task(updates.watch(heard.append, every=0.01))
        await asyncio.sleep(0.05)
        assert heard == []
        (state / "update-request").write_text("check\n")  # queued
        await asyncio.sleep(0.05)
        write_status(state, at=int(time.time()) + 5)  # done
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(run())
    assert heard == [{"type": "updates", "state": "running"}, {"type": "updates", "state": "done"}]


def test_the_dashboard_socket_says_which_version_runs(state, session):
    """A page rendered by another version reloads itself (ws.js)."""
    client = TestClient(app)
    assert 'data-version="0.1.0"' in client.get("/").text
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "state"
        assert socket.receive_json() == {"type": "hello", "version": "0.1.0"}
