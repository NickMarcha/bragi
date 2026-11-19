"""Which USB audio card is the main headset (bragi#59).

Bragi used to take "the first USB output/input" wherever it needed the
headset. With a desk mic like the AT2020USB+ (which also has a headphone
jack) that picked the wrong device three times on one day: the desktop
mic, the phones' listen feed, and WirePlumber's default output after a
replug. Now there's one choice, saved in data/headset_choice.yaml:

- "auto" (the default): the card of a HyperX dongle Bragi recognises by
  its USB serial (headset_hid.find_devices, read from sysfs, so it works at
  startup), else the first USB card with both an output and an input;
- or a card id the user picked in Settings.

Everything that needs "the headset" asks here: the managed peers config
(peers.py), the desktop mic (microphone.py). supervise() keeps the system
default output on the headset, so whatever plays to the default (peers'
incoming audio) goes there, and a newly plugged device can't take over.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import yaml

from . import peers, pipewire

logger = logging.getLogger("bragi.headset_choice")

SETTINGS_FILE = peers.DATA_DIR / "headset_choice.yaml"
AUTO = "auto"


def saved() -> str:
    try:
        data = yaml.safe_load(SETTINGS_FILE.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return AUTO
    card = data.get("card")
    return card if isinstance(card, str) and card else AUTO


def save(card: str) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    pending = SETTINGS_FILE.with_suffix(".tmp")
    pending.write_text(yaml.safe_dump({"card": card}))
    pending.replace(SETTINGS_FILE)


def _usb_cards(nodes) -> dict[str, dict[str, str]]:
    """card id -> {"output": node name, "input": node name} for USB audio cards."""
    from . import headsets
    cards: dict[str, dict[str, str]] = {}
    for n in nodes:
        for media_class, prefix, key in (("Audio/Sink", "alsa_output.", "output"), ("Audio/Source", "alsa_input.", "input")):
            if n.media_class == media_class:
                card = headsets._card_id(n.name, prefix)
                if card:
                    cards.setdefault(card, {})[key] = n.name
    return cards


def _hyperx_card(cards) -> str | None:
    from . import headset_hid  # headset_hid imports peers
    serials = [s for s in headset_hid.find_devices().values() if s]
    return next((c for c in cards if any(headset_hid.serial_matches(c, s) for s in serials)), None)


def card(nodes=None) -> str | None:
    """The main headset's card id, or None without any USB audio card."""
    nodes = pipewire.dump().nodes if nodes is None else nodes
    cards = _usb_cards(nodes)
    choice = saved()
    if choice != AUTO and choice in cards:
        return choice
    if found := _hyperx_card(cards):
        return found
    both = [c for c, io in sorted(cards.items()) if "output" in io and "input" in io]
    return both[0] if both else (sorted(cards)[0] if cards else None)


def output_name(nodes=None) -> str | None:
    nodes = pipewire.dump().nodes if nodes is None else nodes
    c = card(nodes)
    return _usb_cards(nodes).get(c, {}).get("output") if c else None


def input_name(nodes=None) -> str | None:
    nodes = pipewire.dump().nodes if nodes is None else nodes
    c = card(nodes)
    return _usb_cards(nodes).get(c, {}).get("input") if c else None


def view() -> dict:
    """For Settings: the choice and the USB cards to choose from."""
    from . import headsets
    graph = pipewire.dump()
    labels = {h.key: h.label for h in headsets.list_headsets(graph)}
    cards = _usb_cards(graph.nodes)
    options = [{"key": c, "label": labels.get(c, c), "output": "output" in io, "input": "input" in io}
               for c, io in sorted(cards.items())]
    choice = saved()
    current = card(graph.nodes)
    return {"choice": choice, "current": current, "current_label": labels.get(current, current) if current else None,
            "options": options, "missing": choice != AUTO and choice not in cards}


def choose(choice: str) -> None:
    """Saves the choice and puts it to use: the peers config is rewritten
    (phones hear the new headset), the mic routing and the default output follow."""
    from . import microphone
    if choice != AUTO and choice not in _usb_cards(pipewire.dump().nodes):
        raise ValueError("That audio device isn't connected.")
    global _wanted
    save(choice)
    _wanted = None
    peers.sync_managed_conf()
    microphone.reconcile()
    keep_default_output()


def _default_sink() -> str | None:
    try:
        out = pipewire._run(["pw-metadata", "-n", "default", "0", "default.audio.sink"])
    except pipewire.PipewireError:
        return None
    # update: id:0 key:'default.audio.sink' value:'{"name":"alsa_output…"}' type:'Spa:String:JSON'
    match = re.search(r"value:'(\{.*?\})' type:", out)
    if not match:
        return None
    try:
        return json.loads(match.group(1)).get("name")
    except (ValueError, AttributeError):
        return None


# The headset's output name from the last full check, so the 5 s loop only
# runs pw-dump (85 ms on a Pi 4) when the default differs or once a minute.
_wanted: tuple[float, str | None] | None = None


def keep_default_output() -> bool:
    """Makes the headset the default output if it isn't. True when it changed it."""
    global _wanted
    import time
    try:
        current = _default_sink()
        if _wanted is not None and time.monotonic() - _wanted[0] < 60 and current == _wanted[1]:
            return False
        graph = pipewire.dump()
        wanted = output_name(graph.nodes)
        _wanted = (time.monotonic(), wanted)
        if wanted is None or current == wanted:
            return False
        node_id = next((n.id for n in graph.nodes if n.name == wanted), None)
        if node_id is None:
            return False
        pipewire._run(["wpctl", "set-default", str(node_id)])
    except pipewire.PipewireError as exc:
        logger.debug("default output not checked: %s", exc)
        return False
    _wanted = None
    logger.info("default output set to the headset (%s)", wanted)
    return True


async def supervise() -> None:
    """Runs forever as a lifespan task: puts the default output back on the
    headset when something else took it (a newly plugged device)."""
    while True:
        try:
            await asyncio.to_thread(keep_default_output)
        except Exception:
            logger.exception("could not keep the default output on the headset")
        await asyncio.sleep(5)
