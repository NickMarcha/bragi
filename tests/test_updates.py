"""Updates for a packaged Bragi (app/updates.py and packaging/bragi-update):
Bragi asks the root updater through a request file and shows what it
wrote back."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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
    assert "Update now" not in page


def test_a_newer_package_offers_update_now_which_asks_the_updater(state, session):
    write_status(state, packages={"bragi": {"installed": "0.1.0", "candidate": "0.2.0"}})
    client = TestClient(app)
    page = client.get("/updates").text
    assert "bragi 0.2.0" in page and "Update now" in page

    client.post("/updates/upgrade")
    assert (state / "update-request").read_text() == "upgrade\n"


def test_while_the_updater_runs_nothing_else_is_asked_and_the_page_polls(state, session):
    write_status(state, state="running", action="upgrade")
    client = TestClient(app)
    page = client.post("/updates/check").text
    assert "already running" in page and 'hx-trigger="every 3s"' in page
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
