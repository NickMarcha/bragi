"""Back up and restore a hub (app/backup.py, bragi#60)."""

from __future__ import annotations

import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from app import backup, kvm_config, peers
from app.main import app


@pytest.fixture
def hub(session, tmp_path, monkeypatch):
    """A hub with Bragi data (the demo peers), runtime leftovers and a KVM config."""
    kvm = tmp_path / "deskflow-headless"
    (kvm / "tls").mkdir(parents=True)
    (kvm / "backups").mkdir()
    (kvm / "server.ini").write_text("[server]\nexternalConfigFile=/home/olduser/.config/deskflow-headless/server.conf\n")
    (kvm / "server.conf").write_text("section: links\nend\n")
    (kvm / "tls" / "deskflow.pem").write_text("-----BEGIN CERTIFICATE-----\nfake\n")
    (kvm / "backups" / "server.conf.old").write_text("old")
    monkeypatch.setattr(kvm_config, "config_dir", lambda: kvm)
    data = peers.DATA_DIR
    (data / "microphone.json").write_text('{"source": "headset"}')
    (data / "reset-request").write_text("now")
    (data / "peers.conf.bak-x").write_text("old")
    (data / "incidents" / "20261119").mkdir(parents=True)
    (data / "incidents" / "20261119" / "graph.json").write_text("{}")
    calls = []
    monkeypatch.setattr(backup, "_systemctl", lambda *args: calls.append(args))
    monkeypatch.setattr(backup, "restart_bragi", lambda: calls.append(("restart",)))
    return {"kvm": kvm, "data": data, "calls": calls}


def names(archive: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {m.name for m in tar.getmembers()}


def test_a_backup_holds_the_settings_but_not_runtime_state(hub):
    name, archive = backup.backup()
    assert name.startswith("bragi-") and name.endswith(".tar.gz")
    got = names(archive)
    assert {"manifest.json", "data/peers.yaml", "data/microphone.json", "deskflow-headless/server.ini",
            "deskflow-headless/tls/deskflow.pem"} <= got
    assert not any(n.startswith(("data/incidents", "deskflow-headless/backups")) for n in got)
    assert not {"data/reset-request", "data/peers.conf", "data/peers.conf.bak-x"} & got


def test_restoring_on_another_box(hub, tmp_path, monkeypatch):
    _, archive = backup.backup()
    # The other box: empty data and KVM dirs, another home.
    other_data, other_kvm = tmp_path / "other-data", tmp_path / "other-kvm"
    other_data.mkdir()
    monkeypatch.setattr(peers, "DATA_DIR", other_data)
    monkeypatch.setattr(kvm_config, "config_dir", lambda: other_kvm)
    monkeypatch.setattr(backup.Path, "home", staticmethod(lambda: backup.Path("/home/newuser")))
    archive = _with_home(archive, "/home/olduser")
    manifest = backup.restore(archive)
    assert manifest["files"]["data"] >= 2
    assert (other_data / "peers.yaml").read_text() == (hub["data"] / "peers.yaml").read_text()
    assert (other_kvm / "tls" / "deskflow.pem").exists()
    assert "externalConfigFile=/home/newuser/.config/deskflow-headless/server.conf" in (other_kvm / "server.ini").read_text()
    assert list((other_data / "backups").glob("pre-restore-*.tar.gz"))      # what was here is kept
    assert backup.paused()
    assert ("stop", *backup.PAUSED_UNITS) in hub["calls"]
    backup.enforce_pause()
    assert hub["calls"].count(("stop", *backup.PAUSED_UNITS)) == 2         # stays paused on restart
    backup.take_over()
    assert not backup.paused() and ("start", *backup.PAUSED_UNITS) in hub["calls"]


def _with_home(archive: bytes, home: str) -> bytes:
    """The archive with its manifest's home set (backup() records the real one)."""
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as src, tarfile.open(fileobj=out, mode="w:gz") as dst:
        for m in src.getmembers():
            content = src.extractfile(m).read()
            if m.name == "manifest.json":
                manifest = json.loads(content)
                manifest["home"] = home
                content = json.dumps(manifest).encode()
                m.size = len(content)
            dst.addfile(m, io.BytesIO(content))
    return out.getvalue()


def _archive(files: dict[str, bytes], manifest: dict | None = {"format": 1}) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        if manifest is not None:
            files = {"manifest.json": json.dumps(manifest).encode(), **files}
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return out.getvalue()


@pytest.mark.parametrize("archive,match", [
    (_archive({"data/../../etc/passwd": b"x"}), "unexpected path"),
    (_archive({"/etc/cron.d/x": b"x"}), "unexpected path"),
    (_archive({"other/thing": b"x"}), "unexpected path"),
    (_archive({"data/peers.yaml": b"[]"}, manifest=None), "no manifest"),
    (_archive({"data/peers.yaml": b"[]"}, manifest={"format": 99}), "newer Bragi"),
    (b"not a tarball", "Not a Bragi backup"),
])
def test_anything_else_is_refused_before_a_file_changes(hub, archive, match):
    before = (hub["data"] / "peers.yaml").read_text()
    with pytest.raises(backup.BackupError, match=match):
        backup.restore(archive)
    assert (hub["data"] / "peers.yaml").read_text() == before and not backup.paused()


def test_the_settings_section(hub):
    client = TestClient(app)
    assert "Download backup" in client.get("/backup").text
    response = client.get("/backup/download")
    assert response.headers["content-type"] == "application/gzip"
    assert "attachment" in response.headers["content-disposition"]
    page = client.post("/backup/restore", files={"archive": ("b.tar.gz", response.content, "application/gzip")}).text
    assert "Restored the backup" in page and "Paused after a restore" in page and ("restart",) in hub["calls"]
    page = client.post("/backup/take-over").text
    assert "running the peers and the KVM now" in page and not backup.paused()
    assert "Not a Bragi backup" in client.post("/backup/restore", files={"archive": ("x", b"nope", "application/gzip")}).text
