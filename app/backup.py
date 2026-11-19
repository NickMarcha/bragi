"""Back up and restore a hub, for moving Bragi to another box (bragi#60).

Moving the hub from a Pi to an x86 box meant copying Bragi's data, the
KVM's config (with the certificate the Deskflow clients trust) and
hand-written PipeWire configs by hand, and masking services so the new box
wouldn't send audio to the same peers as the old one. Now:

- backup() builds one .tar.gz: data/ (Bragi's data directory, without
  runtime state and old snapshots), deskflow-headless/ (the KVM's config,
  tls/ included), and manifest.json. It holds the dashboard's logins
  (auth.yaml), so the dashboard says to keep it private.
- restore() takes that archive on the new box: checks every path, keeps a
  copy of what it replaces (data/backups/pre-restore-<stamp>.tar.gz), moves
  the KVM config's absolute paths to this user's home, and leaves the hub
  paused: the peers bridge and the KVM server stay stopped, across
  restarts too, until take_over(). So two hubs never send audio to the same
  peers. Bragi then restarts itself to load what was restored.

Peers' managed config (peers.conf) isn't in the archive: Bragi writes it
from peers.yaml on start, with this box's headset.
"""

from __future__ import annotations

import io
import json
import logging
import os
import socket
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath

from . import kvm_config, peers

logger = logging.getLogger("bragi.backup")

FORMAT = 1
MAX_UPLOAD = 50 * 1024 * 1024
# Not worth carrying to another box: runtime state, snapshots, old backups,
# and the managed config Bragi writes from peers.yaml.
DATA_SKIP_DIRS = {"backups", "incidents"}
DATA_SKIP_FILES = {"reset-request", "batteries.json", "peers.conf", "paused"}
KVM_SKIP_DIRS = {"backups"}
PAUSED_UNITS = ("bragi-peers.path", "bragi-peers.service", "deskflow-headless.service")


class BackupError(Exception):
    """Something the dashboard can show as it is."""


def _version() -> str | None:
    from . import updates
    return updates.running_version()


def _skip(rel: PurePosixPath, skip_dirs: set[str], skip_files: set[str]) -> bool:
    name = rel.name
    return (rel.parts[0] in skip_dirs or name in skip_files or name.endswith(".tmp") or ".bak" in name)


def _add_tree(tar: tarfile.TarFile, root: Path, prefix: str, skip_dirs: set[str], skip_files: set[str]) -> int:
    count = 0
    if not root.is_dir():
        return 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = PurePosixPath(path.relative_to(root).as_posix())
        if _skip(rel, skip_dirs, skip_files):
            continue
        tar.add(path, arcname=f"{prefix}/{rel}", recursive=False)
        count += 1
    return count


def backup() -> tuple[str, bytes]:
    """(file name, the .tar.gz)."""
    buf = io.BytesIO()
    host = socket.gethostname()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _add_tree(tar, peers.DATA_DIR, "data", DATA_SKIP_DIRS, DATA_SKIP_FILES)
        kvm = _add_tree(tar, kvm_config.config_dir(), "deskflow-headless", KVM_SKIP_DIRS, set())
        manifest = json.dumps({"format": FORMAT, "bragi": _version(), "host": host, "created": int(time.time()),
                               "home": Path.home().as_posix(), "files": {"data": data, "deskflow-headless": kvm}},
                              indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(manifest))
    return f"bragi-{host}-{stamp}.tar.gz", buf.getvalue()


def _members(tar: tarfile.TarFile) -> tuple[dict, list[tarfile.TarInfo]]:
    """The manifest and the file members, after checking every path."""
    manifest = None
    files = []
    for m in tar.getmembers():
        path = PurePosixPath(m.name)
        if m.name == "manifest.json" and m.isfile():
            manifest = json.loads(tar.extractfile(m).read())
            continue
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] not in ("data", "deskflow-headless"):
            raise BackupError(f"Not a Bragi backup: unexpected path {m.name!r}.")
        if m.isdir():
            continue
        if not m.isfile():
            raise BackupError(f"Not a Bragi backup: {m.name!r} isn't a regular file.")
        files.append(m)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("Not a Bragi backup (no manifest), or one from a newer Bragi.")
    return manifest, files


def restore(archive: bytes) -> dict:
    """Restores a backup() archive here and pauses the hub. Returns the manifest."""
    if len(archive) > MAX_UPLOAD:
        raise BackupError("That file is too big to be a Bragi backup.")
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
        manifest, files = _members(tar)
    except (tarfile.TarError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise BackupError(f"Not a Bragi backup: {exc}") from exc

    # What's here now, in case the restore was a mistake.
    keep_dir = peers.DATA_DIR / "backups"
    keep_dir.mkdir(parents=True, exist_ok=True)
    name, current = backup()
    (keep_dir / f"pre-restore-{name}").write_bytes(current)

    pause()
    roots = {"data": peers.DATA_DIR, "deskflow-headless": kvm_config.config_dir()}
    old_home, new_home = manifest.get("home"), Path.home().as_posix()
    with tar:
        for m in files:
            parts = PurePosixPath(m.name).parts
            target = roots[parts[0]].joinpath(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            content = tar.extractfile(m).read()
            if parts[0] == "deskflow-headless" and target.name == "server.ini" and old_home and old_home != new_home:
                content = content.replace(old_home.encode() + b"/", new_home.encode() + b"/")
            pending = target.with_name(target.name + ".restore-tmp")
            pending.write_bytes(content)
            os.chmod(pending, m.mode & 0o777 or 0o600)
            pending.replace(target)
    logger.info("restored a backup from %s (%s files); paused", manifest.get("host"), len(files))
    return manifest


# --- paused --------------------------------------------------------------------------

def _paused_file() -> Path:
    """Looked up each time: DATA_DIR can be set after import (tests, BRAGI_DATA_DIR)."""
    return peers.DATA_DIR / "paused"


def paused() -> bool:
    return _paused_file().exists()


def _systemctl(*args: str) -> None:
    try:
        subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("systemctl --user %s failed: %s", " ".join(args), exc)


def pause() -> None:
    """Stops the peers bridge and the KVM server and keeps them stopped."""
    _paused_file().parent.mkdir(parents=True, exist_ok=True)
    _paused_file().write_text(f"{int(time.time())}\n")
    _systemctl("stop", *PAUSED_UNITS)


def enforce_pause() -> None:
    """At startup: a paused hub stays paused (the units are enabled, so they
    start with the session)."""
    if paused():
        _systemctl("stop", *PAUSED_UNITS)


def take_over() -> None:
    """Ends the pause: writes the peers' managed config and starts the
    peers bridge and the KVM server."""
    _paused_file().unlink(missing_ok=True)
    peers.sync_managed_conf()
    _systemctl("start", *PAUSED_UNITS)


def restart_bragi() -> None:
    """Restarts this Bragi in two seconds, so the response gets out first."""
    try:
        subprocess.Popen(["systemd-run", "--user", "--collect", "--on-active=2",
                          "systemctl", "--user", "restart", "bragi.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        logger.warning("couldn't schedule a restart: %s", exc)


def view() -> dict:
    return {"paused": paused()}
