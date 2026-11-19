"""A second headset that hears what the main headset hears (bragi#75).

Everything the main headset plays (the computers, the phones, the mic
monitor) reaches it as the box's default output (headset_choice.py). A
second headset gets the same sound from a pw-loopback that captures the
main headset's output and plays it on the second one. That costs a few ms
of extra delay on the second headset and nothing on the main one.

The Pi couldn't do this: its one USB lane couldn't carry two headset
streams (docs/backlog.md). On a box where each headset has its own lane
(Settings > USB) it works.

The setting, in data/second_headset.yaml:

- "off" (the default);
- "auto": another HyperX headset, when one is plugged in and switched on;
- or a card id picked in Settings.

supervise() keeps one pw-loopback running while the setting has both
headsets, restarts it when either one changes, and stops it otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

import yaml

from . import headset_choice, peers, pipewire

logger = logging.getLogger("bragi.second_headset")

SETTINGS_FILE = peers.DATA_DIR / "second_headset.yaml"
OFF, AUTO = "off", "auto"
NODE_NAME = "bragi-second-headset"

_process: subprocess.Popen | None = None
_running_for: tuple[str, str] | None = None


def saved() -> str:
    try:
        data = yaml.safe_load(SETTINGS_FILE.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return OFF
    card = data.get("card")
    return card if isinstance(card, str) and card else OFF


def save(card: str) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    pending = SETTINGS_FILE.with_suffix(".tmp")
    pending.write_text(yaml.safe_dump({"card": card}))
    pending.replace(SETTINGS_FILE)


def _labels(graph) -> dict[str, str]:
    from . import headsets
    return {h.key: h.label for h in headsets.list_headsets(graph)}


def card(graph=None) -> str | None:
    """The second headset's card id, or None (off, or not connected)."""
    graph = pipewire.dump() if graph is None else graph
    choice = saved()
    if choice == OFF:
        return None
    main = headset_choice.card(graph.nodes)
    outputs = {c for c, io in headset_choice._usb_cards(graph.nodes).items() if "output" in io and c != main}
    if choice != AUTO:
        return choice if choice in outputs else None
    labels = _labels(graph)
    hyperx = sorted(c for c in outputs if "hyperx" in (c + " " + labels.get(c, "")).lower())
    return hyperx[0] if hyperx else None


def wanted(graph=None) -> tuple[str, str] | None:
    """(main headset output, second headset output) node names, or None."""
    graph = pipewire.dump() if graph is None else graph
    second = card(graph)
    if second is None:
        return None
    main = headset_choice.output_name(graph.nodes)
    out = headset_choice._usb_cards(graph.nodes).get(second, {}).get("output")
    return (main, out) if main and out and main != out else None


def command(main: str, second: str) -> list[str]:
    return ["pw-loopback", "-n", NODE_NAME,
            "--capture-props", f"stream.capture.sink=true target.object={main} node.dont-fallback=true "
                               f"node.name={NODE_NAME}-capture node.description=\"Second headset (copy of {main})\"",
            "--playback-props", f"target.object={second} node.dont-fallback=true node.name={NODE_NAME}-playback "
                                f"node.description=\"Second headset\""]


def _stop() -> None:
    global _process, _running_for
    if _process is not None and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process, _running_for = None, None


def reconcile(graph=None) -> str:
    """Starts, restarts or stops the loopback to match the setting. Returns
    what it's doing, for the log and Settings."""
    global _process, _running_for
    if saved() == OFF and _process is None:
        return "off"  # nothing to do, and no pw-dump every 5 s
    try:
        target = wanted(graph)
    except pipewire.PipewireError as exc:
        logger.debug("second headset not checked: %s", exc)
        return "unknown"
    alive = _process is not None and _process.poll() is None
    if target is None:
        if alive or _process is not None:
            logger.info("second headset: stopped")
        _stop()
        return "off"
    if alive and _running_for == target:
        return "playing"
    if _process is not None:
        _stop()
    try:
        _process = subprocess.Popen(command(*target), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        logger.warning("second headset: could not start pw-loopback: %s", exc)
        return "failed"
    _running_for = target
    logger.info("second headset: %s plays what %s plays", target[1], target[0])
    return "playing"


def view() -> dict:
    graph = pipewire.dump()
    labels = _labels(graph)
    main = headset_choice.card(graph.nodes)
    options = [{"key": c, "label": labels.get(c, c)}
               for c, io in sorted(headset_choice._usb_cards(graph.nodes).items()) if "output" in io and c != main]
    choice = saved()
    current = card(graph)
    return {"choice": choice, "current": current, "current_label": labels.get(current, current) if current else None,
            "options": options, "missing": choice not in (OFF, AUTO) and current is None,
            "playing": _process is not None and _process.poll() is None}


def choose(choice: str) -> None:
    if choice not in (OFF, AUTO):
        graph = pipewire.dump()
        if choice not in headset_choice._usb_cards(graph.nodes):
            raise ValueError("That audio device isn't connected.")
        if choice == headset_choice.card(graph.nodes):
            raise ValueError("That's the main headset.")
    save(choice)
    reconcile()


async def supervise() -> None:
    """Runs forever as a lifespan task: follows headsets being plugged in,
    switched on and off, and the main headset changing."""
    try:
        while True:
            try:
                await asyncio.to_thread(reconcile)
            except Exception:
                logger.exception("could not set up the second headset")
            await asyncio.sleep(5)
    finally:
        _stop()
