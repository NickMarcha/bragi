"""Settings > Setup: is this host set up for Bragi, and fixes for what isn't.

Moving Bragi to a new box (bragi#58) needed host steps that nothing
mentioned: PipeWire's Roc and VBAN modules, lingering, the input group, the
HyperX dongle's usbcore quirk, tailscale serve, user services that were
enabled but never started. Each check here says what's wrong and offers a
fix where Bragi can make one.

Fixes that need root go through the package's root helper
(packaging/bragi-setup, bragi-setup.service), the same way updates do:
this writes "<action> <user>" to /var/lib/bragi/setup-request, a path unit
starts the helper, and it reports to /var/lib/bragi/setup.json. The helper
only acts for a user that runs Bragi. Fixes that don't need root (starting
user services) Bragi does itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from glob import glob
from pathlib import Path

from . import updates

STATE_DIR = updates.STATE_DIR
HELPER_UNIT = Path("/usr/lib/systemd/system/bragi-setup.path")
SYS = Path(os.environ.get("BRAGI_SYS", "/sys"))
PROC = Path(os.environ.get("BRAGI_PROC", "/proc"))
LINGER_DIR = Path("/var/lib/systemd/linger")
HYPERX_QUIRK = "03f0:06be:r"
BRAGI_URL = "http://127.0.0.1:8000"
MODULE_DIRS = ("/usr/lib/*/pipewire-0.3", "/usr/lib/pipewire-0.3", "/usr/lib64/pipewire-0.3")
ROOT_ACTIONS = ("packages", "linger", "input-group", "pipewire-group", "hyperx-quirk", "tailscale-serve")
# A request the helper hasn't answered after this long means it isn't listening.
QUEUE_TIMEOUT = 60


class SetupError(Exception):
    """Something the dashboard can show as it is."""


def _run(cmd: list[str], timeout: float = 5) -> tuple[int, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return res.returncode, res.stdout


def _user() -> str:
    import pwd  # Linux only; imported here so the app still loads elsewhere
    return pwd.getpwuid(os.getuid()).pw_name


def _check(id_: str, title: str, status: str, detail: str, fix: dict | None = None) -> dict:
    """status: "ok", "problem" or "na" (doesn't apply here)."""
    return {"id": id_, "title": title, "status": status, "detail": detail, "fix": fix if status == "problem" else None}


def _root_fix(action: str, label: str) -> dict:
    return {"kind": "root", "action": action, "label": label}


# --- the checks -------------------------------------------------------------------

def check_pipewire() -> dict:
    rc, out = _run(["systemctl", "--user", "is-active", "pipewire", "wireplumber"])
    states = out.split()
    if states == ["active", "active"]:
        return _check("pipewire", "PipeWire session", "ok", "PipeWire and WirePlumber run for this user.")
    return _check("pipewire", "PipeWire session", "problem",
                  f"PipeWire/WirePlumber aren't running for this user ({' '.join(states) or 'unknown'}). "
                  "Bragi routes all audio through them.",
                  {"kind": "user", "action": "start-pipewire", "label": "Start them"})


def _module(name: str) -> bool:
    return any(glob(f"{d}/libpipewire-module-{name}.so") for d in MODULE_DIRS)


def check_modules() -> dict:
    missing = [m for m in ("roc-sink", "roc-source", "vban-send", "vban-recv") if not _module(m)]
    if not missing:
        return _check("modules", "Roc and VBAN modules", "ok", "PipeWire can send and receive Roc and VBAN audio.")
    return _check("modules", "Roc and VBAN modules", "problem",
                  f"Missing PipeWire modules: {', '.join(missing)}. Linux peers use Roc, Windows peers VBAN.",
                  _root_fix("packages", "Install them"))


def check_linger() -> dict:
    user = _user()
    if (LINGER_DIR / user).exists():
        return _check("linger", "Runs without a login", "ok", f"Lingering is on for {user}: Bragi starts at boot.")
    return _check("linger", "Runs without a login", "problem",
                  f"Lingering is off for {user}, so Bragi and PipeWire stop at logout and don't start at boot "
                  "until someone logs in.", _root_fix("linger", "Turn lingering on"))


def check_input_group() -> dict:
    import grp, pwd
    user = _user()
    try:
        group = grp.getgrnam("input")
    except KeyError:
        return _check("input", "Input devices", "na", "This system has no input group.")
    if user in group.gr_mem or pwd.getpwnam(user).pw_gid == group.gr_gid:
        active = group.gr_gid in os.getgroups()
        return _check("input", "Input devices", "ok" if active else "problem",
                      f"{user} is in the input group." if active else
                      f"{user} was added to the input group, but Bragi still runs without it. Restart the box "
                      "(or log out everywhere) for it to apply.")
    return _check("input", "Input devices", "problem",
                  f"{user} isn't in the input group, so Bragi can't read the headset's volume knob and the KVM "
                  "can't read the keyboard and mouse.", _root_fix("input-group", "Add to input"))


def check_realtime() -> dict:
    """PipeWire needs real-time priority, or a busy moment on a small box
    makes the audio click. Debian grants it to the pipewire group
    (/etc/security/limits.d/25-pw-rlimits.conf); bragi#71."""
    import resource
    title = "Real-time audio"
    soft, _ = resource.getrlimit(resource.RLIMIT_RTPRIO)
    if soft > 0:
        return _check("realtime", title, "ok", f"PipeWire may use real-time priority (up to {soft}).")
    try:
        import grp
        group = grp.getgrnam("pipewire")
    except (ImportError, KeyError):
        return _check("realtime", title, "problem",
                      "This session has no real-time priority, so PipeWire competes for the CPU and the audio can "
                      "click when the box is busy. Set an rtprio limit for this user (or run rtkit).")
    if _user() in group.gr_mem:
        return _check("realtime", title, "problem",
                      f"{_user()} is in the pipewire group, but this session started before that. Restart the box "
                      "(or log out everywhere) so PipeWire gets real-time priority.")
    return _check("realtime", title, "problem",
                  f"{_user()} isn't in the pipewire group, so PipeWire runs without real-time priority and the "
                  "audio clicks when the box is busy.", _root_fix("pipewire-group", "Add to pipewire"))


def _hyperx_dongles() -> list[Path]:
    out = []
    for dev in (SYS / "bus/usb/devices").glob("*"):
        try:
            if (dev / "idVendor").read_text().strip() == "03f0" and (dev / "idProduct").read_text().strip() == "06be":
                out.append(dev)
        except OSError:
            continue
    return out


def check_hyperx_quirk() -> dict:
    title = "HyperX dongle quirk"
    if not _hyperx_dongles():
        return _check("hyperx", title, "na", "No HyperX Cloud III S dongle is plugged in.")
    try:
        runtime = HYPERX_QUIRK in (SYS / "module/usbcore/parameters/quirks").read_text()
    except OSError:
        runtime = False
    try:
        boot = HYPERX_QUIRK in (PROC / "cmdline").read_text()
    except OSError:
        boot = False
    if boot:
        return _check("hyperx", title, "ok", "The kernel reads the dongle's full descriptor, so its controls work.")
    if runtime:
        return _check("hyperx", title, "ok",
                      "Set for this boot. If the headset controls don't respond, replug the dongle. Bragi's fix "
                      "also makes it permanent.")
    return _check("hyperx", title, "problem",
                  "Without usbcore.quirks=03f0:06be:r Linux reads the dongle's descriptor in a way that stops it "
                  "forwarding anything from the headset: battery, mute, EQ and the macro button do nothing.",
                  _root_fix("hyperx-quirk", "Set the quirk"))


def check_tailscale_serve() -> dict:
    title = "Dashboard over Tailscale"
    if not shutil.which("tailscale"):
        return _check("tailscale", title, "na", "Tailscale isn't installed; the dashboard is on 127.0.0.1:8000 only.")
    rc, out = _run(["tailscale", "serve", "status", "--json"])
    try:
        served = BRAGI_URL in json.dumps(json.loads(out or "{}"))
    except ValueError:
        served = False
    if served:
        return _check("tailscale", title, "ok", "tailscale serve forwards HTTPS on the tailnet to Bragi.")
    return _check("tailscale", title, "problem",
                  "tailscale serve doesn't point at Bragi, so phones and other computers can't reach the "
                  "dashboard or the peer socket.", _root_fix("tailscale-serve", "Serve Bragi"))


HUB_UNITS = ("bragi-peers.path", "bragi-peers.service", "bragi-audio-reset.path")


def _hub_units() -> list[str]:
    """Bragi's user services, plus the KVM server when it's installed and set up."""
    units = list(HUB_UNITS)
    if shutil.which("deskflow-core") and (Path.home() / ".config/deskflow-headless/server.ini").exists():
        units.append("deskflow-headless.service")
    return units


def check_services() -> dict:
    units = _hub_units()
    rc, out = _run(["systemctl", "--user", "is-active", *units])
    down = [u for u, s in zip(units, out.split()) if s != "active"]
    rc, out = _run(["systemctl", "--user", "is-enabled", *units])
    off = [u for u, s in zip(units, out.split()) if s not in ("enabled", "enabled-runtime", "static", "alias")]
    if not down and not off:
        return _check("services", "Bragi's services", "ok", "The peer bridge, audio reset and KVM services run, "
                      "and start at boot.")
    parts = []
    if down:
        parts.append(f"Not running: {', '.join(down)}.")
    if off:
        parts.append(f"Not started at boot: {', '.join(off)}.")
    return _check("services", "Bragi's services", "problem", " ".join(parts),
                  {"kind": "user", "action": "start-services", "label": "Enable and start them"})


def check_usb() -> dict:
    """Audio devices sharing a full-speed USB lane (bragi#63)."""
    from . import usb_topology
    title = "USB bandwidth"
    view = usb_topology.view()
    if view["failing"]:
        f = view["failing"][0]
        return _check("usb", title, "problem",
                      f"{f['product']} was refused USB bandwidth {f['count']} times in the last 15 minutes, so it "
                      "stays silent. Move it to a port on another lane (Settings > USB shows them).")
    crowded = [l for l in view["lanes"] if l["audio"] >= view["crowded"]]
    if crowded:
        return _check("usb", title, "problem",
                      f"{crowded[0]['audio']} audio devices share {crowded[0]['lane']}; they don't all fit. "
                      "Move one to a port on another lane (Settings > USB).")
    tight = [l for l in view["lanes"] if l["audio"] == 2]
    if tight:
        return _check("usb", title, "ok",
                      f"Two audio devices share {tight[0]['lane']}: fine, but a third there wouldn't fit.")
    return _check("usb", title, "ok", "No full-speed USB lane is crowded.")


def check_hand_peers() -> dict:
    """Roc peers still run from a hand-written conf.d file (bragi#62)."""
    from . import peers
    title = "Hand-configured peers"
    report = peers.hand_configured()
    if not report["peers"]:
        return _check("handpeers", title, "na", "Bragi manages every peer.")
    blocked = [f for f in report["files"] if f["other"]]
    if blocked:
        f = blocked[0]
        return _check("handpeers", title, "problem",
                      f"{', '.join(report['peers'])} run from {f['path']}, which also runs {', '.join(f['other'])}. "
                      "Bragi can only take over a file whose Roc modules are all its peers'.")
    if not report["files"]:
        return _check("handpeers", title, "problem",
                      f"{', '.join(report['peers'])} are configured outside Bragi, but not in "
                      f"{peers.PIPEWIRE_CONF_DIR}, so Bragi can't take them over.")
    files = ", ".join(Path(f["path"]).name for f in report["files"])
    return _check("handpeers", title, "problem",
                  f"{', '.join(report['peers'])} run from {files}, which Bragi doesn't write: moving the hub or "
                  "changing the headset means editing it by hand. Bragi can take them over with the same ports "
                  "and names (the computers don't notice), set the file aside, and restart the audio.",
                  {"kind": "user", "action": "adopt-peers", "label": "Let Bragi manage them"})


CHECKS = ("check_pipewire", "check_realtime", "check_modules", "check_linger", "check_input_group", "check_hyperx_quirk",
          "check_tailscale_serve", "check_services", "check_usb", "check_hand_peers")


# check id -> function, so a fix runs only its own check.
CHECK_IDS = {"pipewire": "check_pipewire", "realtime": "check_realtime", "modules": "check_modules", "linger": "check_linger",
             "input": "check_input_group", "hyperx": "check_hyperx_quirk", "tailscale": "check_tailscale_serve",
             "services": "check_services", "usb": "check_usb", "handpeers": "check_hand_peers"}


def run_all() -> list[dict]:
    # Looked up by name at call time, so a test can replace one check.
    return [globals()[name]() for name in CHECKS]


# --- fixes --------------------------------------------------------------------------

def helper_available() -> bool:
    return HELPER_UNIT.exists()


def status() -> dict:
    try:
        data = json.loads((STATE_DIR / "setup.json").read_text())
    except (OSError, ValueError):
        data = {}
    if data.get("state") == "running" and time.time() - data.get("at", 0) > 15 * 60:
        data["state"] = "failed"
        data["message"] = "The setup helper stopped answering."
    return data


def pending() -> bool:
    """A request the helper hasn't picked up yet."""
    try:
        requested = (STATE_DIR / "setup-request").stat().st_mtime
    except OSError:
        return False
    answered = status().get("at", 0)
    return requested > answered and time.time() - requested < QUEUE_TIMEOUT


def fix(check_id: str) -> str:
    """Runs or requests the fix for one check. Returns what happened."""
    name = CHECK_IDS.get(check_id)
    if name is None:
        raise SetupError("Unknown check.")
    check = globals()[name]()
    action = check["fix"]
    if action is None:
        return "Nothing to fix."
    if action["kind"] == "user" and action["action"] == "adopt-peers":
        from . import audio_reset, peers
        try:
            names = peers.adopt_hand_configured()
        except ValueError as exc:
            raise SetupError(str(exc)) from exc
        try:
            audio_reset.request()
        except RuntimeError:
            return f"Bragi manages {', '.join(names)} now. An audio reset is already running; it picks them up."
        return f"Bragi manages {', '.join(names)} now. The audio restarts for a few seconds."
    if action["kind"] == "user":
        units = ["pipewire", "wireplumber", "pipewire-pulse"] if action["action"] == "start-pipewire" else _hub_units()
        rc, _ = _run(["systemctl", "--user", "enable", "--now", *units], timeout=30)
        if rc != 0:
            raise SetupError("Starting them failed; see `systemctl --user status` on the box.")
        return "Started."
    if not helper_available():
        raise SetupError("This Bragi wasn't installed as a package, so there's no setup helper. "
                         "The check's text says what to do by hand.")
    if status().get("state") == "running" or pending():
        raise SetupError("The setup helper is busy with another fix.")
    if action["action"] not in ROOT_ACTIONS:
        raise SetupError("Unknown fix.")
    try:
        with open(STATE_DIR / "setup-request", "w") as f:
            f.write(f"{action['action']} {_user()}\n")
    except OSError as exc:
        raise SetupError(f"Can't reach the setup helper: {exc}") from exc
    return "Asked the setup helper; this updates when it's done."


def view() -> dict:
    return {"checks": run_all(), "helper": helper_available(), "status": status(), "pending": pending()}
