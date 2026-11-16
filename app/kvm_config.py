"""The KVM's settings, owned by Bragi: which computers share the desk's
keyboard and mouse, where they sit, the hotkeys, and the input options.

The engine is the headless Deskflow server. Its files live in
~/.config/deskflow-headless/ (the package's default):

- server.ini   QSettings: the computers ([computer_<name>] name/aliases),
               this host's own name ([core] computerName), where server.conf is
- server.conf  the links between computers and the options (hotkeys)
- env          input options, read at startup
- webui-layout.json  the grid positions links are built from

Saving the layout or hotkeys rewrites server.conf and asks the running
server to reload it (SIGHUP); the server reports whether it took it in its
status file, and if not, the previous file goes back. Saving computers or
input options needs a restart (the server reads them at startup); if it
doesn't come back up, the previous files go back. Each save keeps the last
KEEP_BACKUPS versions of what it changes in backups/.

Ported from the fork's own setup page (NickMarcha/deskflow, headless/webui),
which this replaces. Unlike it, saving input options keeps env keys it
doesn't manage (e.g. DESKFLOW_EVDEV_HIDPP).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import kvm

UNIT = "deskflow-headless.service"
KEEP_BACKUPS = 20
DIRS = ("left", "right", "up", "down")
STEP = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}
INPUT_KEYS = (
    "DESKFLOW_EVDEV_MATCH",
    "DESKFLOW_EVDEV_IGNORE",
    "DESKFLOW_EVDEV_SHAPE",
    "DESKFLOW_EVDEV_ACCEL",
    "DESKFLOW_EVDEV_REPEAT",
    "XKB_DEFAULT_LAYOUT",
)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

_lock = threading.Lock()


class ConfigError(Exception):
    """Something the dashboard can show as it is."""


def config_dir() -> Path:
    return Path(os.environ.get("BRAGI_KVM_CONFIG_DIR") or Path.home() / ".config" / "deskflow-headless")


# --- files ---------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _restore(path: Path, text: str | None) -> None:
    if text is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, text)


def _backup(path: Path) -> None:
    text = _read(path)
    if text is None:
        return
    backups = config_dir() / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    (backups / f"{path.name}.{stamp}").write_text(text, encoding="utf-8")
    for stale in sorted(backups.glob(f"{path.name}.*"))[:-KEEP_BACKUPS]:
        stale.unlink(missing_ok=True)


# --- server.ini (QSettings) -------------------------------------------------------

def ini_unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def ini_quote(value: str) -> str:
    if re.search(r'[,;"=\\#]|^\s|\s$', value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def ini_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        m = re.match(r"^\[(.+)\]\s*$", line)
        if m:
            sections.append((m.group(1), []))
        else:
            sections[-1][1].append(line)
    return sections


def ini_values(lines: list[str]) -> dict[str, str]:
    out = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith((";", "#")):
            key, value = line.split("=", 1)
            out[key.strip()] = ini_unquote(value)
    return out


def ini_join(sections: list[tuple[str, list[str]]]) -> str:
    out: list[str] = []
    for name, lines in sections:
        if name:
            out.append(f"[{name}]")
        out.extend(line for line in lines if line.strip())
    return "\n".join(out) + "\n"


# --- server.conf --------------------------------------------------------------------

def parse_conf(text: str) -> tuple[dict[str, dict[str, str]], list[str], list[str]]:
    """(links, option lines, other sections kept verbatim)."""
    links: dict[str, dict[str, str]] = {}
    options: list[str] = []
    other: list[str] = []
    section = None
    current = None
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if section is None:
            m = re.match(r"section:\s*(\w+)", line)
            if m:
                section, current, buf = m.group(1), None, [raw]
            continue
        if line == "end":
            if section not in ("links", "options"):
                other.append("\n".join([*buf, raw]))
            section = None
            continue
        if section == "links":
            if line.endswith(":"):
                current = line[:-1].strip()
                links.setdefault(current, {})
                continue
            m = re.match(r"(left|right|up|down)(\([^)]*\))?\s*=\s*([^\s(]+)", line)
            if m and current:
                links[current][m.group(1)] = m.group(3)
        elif section == "options":
            options.append(line)
        else:
            buf.append(raw)
    return links, options, other


def derive_cells(links: dict[str, dict[str, str]], names: list[str]) -> dict[str, list[int]]:
    """Grid positions from the links, when no layout file exists yet."""
    cells: dict[str, tuple[int, int]] = {}
    taken: set[tuple[int, int]] = set()
    for start in [n for n in names if n in links] + [n for n in links if n not in names]:
        if start in cells:
            continue
        col = max((c for c, _ in cells.values()), default=-2) + 2
        queue = [(start, (col, 0))]
        while queue:
            name, pos = queue.pop(0)
            if name in cells or pos in taken:
                continue
            cells[name] = pos
            taken.add(pos)
            for d, target in links.get(name, {}).items():
                dc, dr = STEP[d]
                queue.append((target, (pos[0] + dc, pos[1] + dr)))
    if not cells:
        return {}
    c0 = min(c for c, _ in cells.values())
    r0 = min(r for _, r in cells.values())
    return {n: [c - c0, r - r0] for n, (c, r) in cells.items()}


def build_conf(cells: dict[str, list[int]], options: list[str], other: list[str]) -> str:
    pos = {tuple(v): k for k, v in cells.items()}
    out = [
        "# Written by Bragi (KVM setup). Saving there rewrites this file;",
        "# computer positions are kept in webui-layout.json next to it.",
        "",
        "section: links",
    ]
    for name in sorted(cells, key=lambda n: (cells[n][1], cells[n][0])):
        c, r = cells[name]
        neighbours = [f"\t\t{d} = {pos[(c + STEP[d][0], r + STEP[d][1])]}"
                      for d in DIRS if (c + STEP[d][0], r + STEP[d][1]) in pos]
        if neighbours:
            out.append(f"\t{name}:")
            out.extend(neighbours)
    out += ["end", "", "section: options"]
    out += [f"\t{o}" for o in options]
    out.append("end")
    for block in other:
        out += ["", block]
    return "\n".join(out) + "\n"


def without_computers(line: str, gone: set[str]) -> str | None:
    """A hotkey line with removed computers taken out of its targets, or None
    when nothing is left for it to do: switchToScreen(old) goes; in
    keystroke(keys,a:b:old) only "old" goes."""
    if not gone:
        return line
    for name in gone:
        if re.search(rf"\(\s*{re.escape(name)}\s*\)", line):
            return None
    m = re.search(r",\s*([A-Za-z0-9._:-]+)\s*\)\s*$", line)
    if m:
        targets = [t for t in m.group(1).split(":") if t and t not in gone]
        if not targets:
            return None
        line = line[:m.start(1)] + ":".join(targets) + line[m.end(1):]
    return line


# --- reading ------------------------------------------------------------------------

def _settings_path() -> Path:
    return config_dir() / "server.ini"


def _sections() -> list[tuple[str, list[str]]]:
    text = _read(_settings_path())
    if text is None:
        raise ConfigError(f"The KVM's settings aren't there ({_settings_path()}). Is deskflow-headless set up?")
    return ini_sections(text)


def _primary(sections) -> str:
    for name, lines in sections:
        if name == "core":
            return ini_values(lines).get("computerName", "")
    return ""


def _computers(sections) -> list[dict]:
    out = []
    for name, lines in sections:
        if name.startswith("computer_"):
            values = ini_values(lines)
            aliases = [a.strip() for a in values.get("aliases", "").split(",") if a.strip()]
            out.append({"name": values.get("name", name[len("computer_"):]), "aliases": aliases})
    return out


def _conf_path(sections) -> Path:
    for name, lines in sections:
        if name == "server":
            path = ini_values(lines).get("externalConfigFile")
            if path:
                return Path(path)
    return config_dir() / "server.conf"


def _layout(sections) -> tuple[dict[str, list[int]], list[str], list[str]]:
    links, options, other = parse_conf(_read(_conf_path(sections)) or "")
    names = [c["name"] for c in _computers(sections)]
    try:
        cells = json.loads(_read(config_dir() / "webui-layout.json") or "{}").get("cells", {})
    except json.JSONDecodeError:
        cells = {}
    if not cells:
        cells = derive_cells(links, names)
    primary = _primary(sections)
    return {n: v for n, v in cells.items() if n in names and n != primary}, options, other


def _env_lines() -> list[str]:
    return (_read(config_dir() / "env") or "").splitlines()


def _input() -> dict[str, str]:
    out = {k: "" for k in INPUT_KEYS}
    for line in _env_lines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            if key.strip() in out:
                out[key.strip()] = value
    return out


def read() -> dict:
    """Everything the KVM setup shows."""
    sections = _sections()
    primary = _primary(sections)
    cells, options, _ = _layout(sections)
    return {
        "host": primary,
        "computers": [c for c in _computers(sections) if c["name"] != primary],
        "cells": cells,
        "hotkeys": options,
        "input": _input(),
    }


# --- the running server ---------------------------------------------------------------

def _service() -> dict:
    try:
        res = subprocess.run(["systemctl", "--user", "show", UNIT, "-p", "ActiveState,MainPID"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)


def _main_pid() -> int:
    try:
        return int(_service().get("MainPID", "0"))
    except ValueError:
        return 0


def _status() -> dict | None:
    try:
        return json.loads((kvm.engine_dir() / "status.json").read_text())
    except (OSError, ValueError):
        return None


def _reload() -> str | None:
    """Asks the server to reload server.conf. None when it took it, else why not."""
    pid = _main_pid()
    if pid <= 0:
        return None  # not running: it reads the file when it starts
    status = _status()
    if status is None:
        return "The KVM has no status file, so a reload can't be checked."
    before = (status.get("configReload") or {}).get("at")
    os.kill(pid, signal.SIGHUP)
    end = time.monotonic() + 5
    while time.monotonic() < end:
        result = (_status() or {}).get("configReload") or {}
        if result.get("at") and result.get("at") != before:
            return None if result.get("ok") else "The KVM rejected the new settings"
        time.sleep(0.1)
    if _main_pid() != pid:
        return "The KVM restarted instead of reloading"
    return "The KVM didn't confirm the reload"


def _restart() -> bool:
    subprocess.run(["systemctl", "--user", "restart", UNIT], capture_output=True, timeout=20)
    for _ in range(30):
        if _service().get("ActiveState") == "active":
            return True
        time.sleep(0.2)
    return False


# --- writing -------------------------------------------------------------------------------

def _check_cells(cells, names: set[str]) -> dict[str, list[int]]:
    if not isinstance(cells, dict):
        raise ConfigError("cells must be an object")
    out, seen = {}, set()
    for name, pos in cells.items():
        if name not in names:
            raise ConfigError(f"Unknown computer: {name}")
        if not (isinstance(pos, list) and len(pos) == 2 and all(isinstance(v, int) and 0 <= v < 32 for v in pos)):
            raise ConfigError(f"Bad position for {name}")
        if tuple(pos) in seen:
            raise ConfigError("Two computers in the same place")
        seen.add(tuple(pos))
        out[name] = pos
    return out


def _check_hotkeys(hotkeys) -> list[str]:
    if not isinstance(hotkeys, list) or not all(isinstance(o, str) for o in hotkeys):
        raise ConfigError("hotkeys must be a list of lines")
    out = []
    for line in hotkeys:
        line = line.strip()
        if not line:
            continue
        if "\n" in line or "\r" in line or line == "end" or line.startswith("section:"):
            raise ConfigError(f"Not allowed in hotkeys: {line!r}")
        out.append(line)
    return out


def save_layout(cells, hotkeys) -> str:
    """Writes the links and hotkeys and reloads them in the running server
    (clients stay connected). Puts the old file back if it's refused."""
    with _lock:
        sections = _sections()
        names = {c["name"] for c in _computers(sections)} - {_primary(sections)}
        cells = _check_cells(cells, names)
        hotkeys = _check_hotkeys(hotkeys)
        conf_path = _conf_path(sections)
        layout_path = config_dir() / "webui-layout.json"
        old_conf, old_layout = _read(conf_path), _read(layout_path)
        _, _, other = parse_conf(old_conf or "")
        _backup(conf_path)
        _atomic_write(conf_path, build_conf(cells, hotkeys, other))
        _atomic_write(layout_path, json.dumps({"cells": cells}, indent=2) + "\n")
        problem = _reload()
        if problem is None:
            return "Applied; the computers stayed connected."
        _restore(conf_path, old_conf)
        _restore(layout_path, old_layout)
        _reload()
        raise ConfigError(f"{problem}, so the previous settings are back.")


def save_computers(computers) -> str:
    """Rewrites the computer list (names and aliases) and restarts the
    server. Positions and hotkeys of removed computers go too."""
    if not isinstance(computers, list):
        raise ConfigError("computers must be a list")
    with _lock:
        sections = _sections()
        primary = _primary(sections)
        seen: set[str] = {primary}
        new_sections = []
        for c in computers:
            if not isinstance(c, dict):
                raise ConfigError("Each computer needs a name")
            name = str(c.get("name", "")).strip()
            aliases = [str(a).strip() for a in c.get("aliases", []) if str(a).strip()]
            for n in [name, *aliases]:
                if not NAME_RE.match(n):
                    raise ConfigError(f"Not a valid computer name: {n!r}")
                if n in seen:
                    raise ConfigError(f"Name used twice: {n}")
                seen.add(n)
            lines = [f"name={name}"]
            if aliases:
                lines.insert(0, f"aliases={ini_quote(', '.join(aliases))}")
            new_sections.append((f"computer_{name}", lines))

        kept = [s for s in sections if not s[0].startswith("computer_") or s[0] == f"computer_{primary}"]
        if not any(s[0] == f"computer_{primary}" for s in kept):
            kept.append((f"computer_{primary}", [f"name={primary}"]))
        names = {str(c.get("name", "")).strip() for c in computers}
        cells, hotkeys, other = _layout(sections)
        cells = {n: p for n, p in cells.items() if n in names}
        gone = {c["name"] for c in _computers(sections)} - names - {primary}
        hotkeys = [h for h in (without_computers(o, gone) for o in hotkeys) if h is not None]

        settings, conf_path, layout_path = _settings_path(), _conf_path(sections), config_dir() / "webui-layout.json"
        old = {p: _read(p) for p in (settings, conf_path, layout_path)}
        _backup(settings)
        _backup(conf_path)
        _atomic_write(settings, ini_join(kept + new_sections))
        _atomic_write(conf_path, build_conf(cells, hotkeys, other))
        _atomic_write(layout_path, json.dumps({"cells": cells}, indent=2) + "\n")
        if _restart():
            return "Saved; the KVM restarted and the computers are reconnecting."
        for path, text in old.items():
            _restore(path, text)
        _restart()
        raise ConfigError("The KVM didn't start with the new computers, so the old ones are back.")


HIDPP_CODE_BASE = 0x1000  # the fork's binding codes for HID++ controls (0x1000 + control id)


def fork_hidpp() -> dict:
    """Whether the KVM server still handles HID++ buttons itself: its
    listener (DESKFLOW_EVDEV_HIDPP=1) and bindings for HID++ controls in
    bindings.json. Both would fight Bragi over the same buttons."""
    listening = any(line.split("=", 1)[0].strip() == "DESKFLOW_EVDEV_HIDPP"
                    and line.split("=", 1)[1].strip().strip('"') not in ("", "0")
                    for line in _env_lines() if "=" in line and not line.lstrip().startswith("#"))
    bindings = []
    try:
        data = json.loads(_read(config_dir() / "bindings.json") or "{}")
        bindings = [b for b in data.get("bindings", []) if isinstance(b, dict)
                    and isinstance(b.get("code"), int) and b["code"] >= HIDPP_CODE_BASE]
    except (ValueError, AttributeError):
        pass
    return {"listening": listening, "bindings": bindings}


def hand_over_hidpp() -> str:
    """Turns the KVM server's HID++ listener off and drops its HID++
    bindings, so Bragi alone handles those buttons. Restarts the server."""
    with _lock:
        env_path = config_dir() / "env"
        bindings_path = config_dir() / "bindings.json"
        old_env, old_bindings = _read(env_path), _read(bindings_path)
        lines = [line for line in _env_lines()
                 if not (not line.lstrip().startswith("#") and line.split("=", 1)[0].strip() == "DESKFLOW_EVDEV_HIDPP")]
        _backup(env_path)
        _atomic_write(env_path, "\n".join(lines).rstrip("\n") + "\n")
        if old_bindings is not None:
            try:
                data = json.loads(old_bindings)
                data["bindings"] = [b for b in data.get("bindings", []) if not (
                    isinstance(b, dict) and isinstance(b.get("code"), int) and b["code"] >= HIDPP_CODE_BASE)]
                _backup(bindings_path)
                _atomic_write(bindings_path, json.dumps(data, indent=2) + "\n")
            except (ValueError, AttributeError):
                pass
        if _restart():
            return "Done: Bragi handles the mouse buttons now; the KVM restarted and the computers are reconnecting."
        _restore(env_path, old_env)
        _restore(bindings_path, old_bindings)
        _restart()
        raise ConfigError("The KVM didn't start again, so its old settings are back.")


def save_input(values) -> str:
    """Writes the input options to env and restarts the server. Keys this
    doesn't manage stay as they are."""
    if not isinstance(values, dict):
        raise ConfigError("input must be an object")
    clean = {}
    for key in INPUT_KEYS:
        value = str(values.get(key, "")).strip()
        if any(ch in value for ch in '"\\\n\r$`'):
            raise ConfigError(f"{key}: quotes, backslashes, $ and backticks aren't allowed")
        clean[key] = value
    if clean["DESKFLOW_EVDEV_SHAPE"] and not re.fullmatch(r"\d{2,5}x\d{2,5}", clean["DESKFLOW_EVDEV_SHAPE"]):
        raise ConfigError("The virtual screen size looks like 1920x1080")
    if clean["DESKFLOW_EVDEV_REPEAT"] and not re.fullmatch(r"\d{1,4},\d{1,3}", clean["DESKFLOW_EVDEV_REPEAT"]):
        raise ConfigError("Key repeat looks like 500,30 (delay ms, rate Hz)")
    if clean["DESKFLOW_EVDEV_ACCEL"]:
        try:
            if not -1.0 <= float(clean["DESKFLOW_EVDEV_ACCEL"]) <= 1.0:
                raise ValueError
        except ValueError:
            raise ConfigError("Pointer speed is between -1 and 1") from None
    with _lock:
        env_path = config_dir() / "env"
        old = _read(env_path)
        kept = [line for line in _env_lines()
                if line.strip() and not line.lstrip().startswith("#")
                and line.split("=", 1)[0].strip() not in INPUT_KEYS]
        lines = ["# Written by Bragi (KVM setup). Read by the KVM server at startup."]
        lines += kept + [f'{k}="{v}"' for k, v in clean.items() if v]
        _backup(env_path)
        _atomic_write(env_path, "\n".join(lines) + "\n")
        if _restart():
            return "Saved; the KVM restarted and the computers are reconnecting."
        _restore(env_path, old)
        _restart()
        raise ConfigError("The KVM didn't start with these input options, so the old ones are back.")
