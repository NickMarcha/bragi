"""Thin subprocess wrapper around pw-dump / wpctl / pw-cli.

Bragi never talks to the PipeWire socket directly - it shells out to the
same CLI tools a human would use, on purpose. That keeps this module small
and lets `wpctl`/`pw-cli` (which already know how to encode Props params
correctly) do the hard part.

The one piece of state here is a volume/mute cache (see _volumes), which
watcher.py keeps honest from pw-mon's event stream.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


class PipewireError(RuntimeError):
    pass


def _run(args: list[str], input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise PipewireError(f"{args[0]} timed out") from exc
    if result.returncode != 0:
        raise PipewireError(
            f"{' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


@dataclass
class Node:
    id: int
    name: str
    description: str
    media_class: str


def _parse_node(obj: dict) -> Node | None:
    props = (obj.get("info") or {}).get("props") or {}
    media_class = props.get("media.class", "")
    if "Audio" not in media_class:
        return None
    node_id = obj["id"]
    return Node(
        id=node_id,
        name=props.get("node.name", f"node-{node_id}"),
        description=props.get("node.description", props.get("node.name", "")),
        media_class=media_class,
    )


_VOLUME_RE = re.compile(r"Volume:\s*([0-9.]+)\s*(\[MUTED\])?")


# Last wpctl reading per node id. Volume and mute only change through
# Bragi's own writes below (each forgets its node once done) or something
# pw-mon reports (watcher.py forgets the id, and everything whenever pw-mon
# itself restarts) - so a reading is good until one of those says
# otherwise, and a page load stops paying a wpctl per fader.
#
# _volume_epoch counts forgets per node. A read only stores its result if
# no forget landed while it was running: one that started before a change
# and finished after it is holding the old value.
_volumes: dict[int, tuple[float, bool]] = {}
_volume_epoch: dict[int, int] = {}
_global_epoch = 0
_volume_lock = threading.Lock()


def _read_volume_mute(node_id: int) -> tuple[float | None, bool]:
    try:
        out = _run(["wpctl", "get-volume", str(node_id)])
    except PipewireError:
        return None, False
    m = _VOLUME_RE.search(out)
    if not m:
        return None, False
    return float(m.group(1)), bool(m.group(2))


def get_volume_mute(node_id: int) -> tuple[float | None, bool]:
    """Volume/mute for a single node, from the cache above when it has one.
    Deliberately not batched - wpctl has no bulk query, and pw-dump's raw
    Props volume uses a different (cubic) scale than what wpctl
    reads/writes, so it can't be substituted here without silently
    drifting from what `wpctl set-volume` actually does.

    A failed read (node gone, wpctl error) is returned but never stored."""
    with _volume_lock:
        cached = _volumes.get(node_id)
        if cached is not None:
            return cached
        epoch = (_global_epoch, _volume_epoch.get(node_id, 0))
    volume, muted = _read_volume_mute(node_id)
    if volume is not None:
        with _volume_lock:
            if (_global_epoch, _volume_epoch.get(node_id, 0)) == epoch:
                _volumes[node_id] = (volume, muted)
    return volume, muted


def forget_volume(node_id: int) -> None:
    with _volume_lock:
        _volumes.pop(node_id, None)
        _volume_epoch[node_id] = _volume_epoch.get(node_id, 0) + 1


def forget_volumes() -> None:
    global _global_epoch
    with _volume_lock:
        _volumes.clear()
        _global_epoch += 1


def get_volume_mute_many(node_ids: set[int]) -> dict[int, tuple[float | None, bool]]:
    """get_volume_mute for several nodes at once, with the ones the cache
    can't answer read concurrently - each is its own wpctl process (~50ms
    on sagepi), so a whole dashboard's worth of them back to back was most
    of the cost of building one."""
    with _volume_lock:
        result = {i: _volumes[i] for i in node_ids if i in _volumes}
    missing = sorted(node_ids - result.keys())
    if missing:
        with ThreadPoolExecutor(max_workers=len(missing)) as pool:
            result.update(zip(missing, pool.map(get_volume_mute, missing)))
    return result


@dataclass
class Device:
    id: int
    name: str  # e.g. "alsa_card.usb-HP__Inc_HyperX_..." - see headsets.py's card_id
    description: str
    active_profile_index: int | None
    off_profile_index: int | None
    restore_profile_index: int | None  # highest-priority non-off profile, to re-enable with


def _parse_device(obj: dict) -> Device | None:
    props = (obj.get("info") or {}).get("props") or {}
    name = props.get("device.name", "")
    if not name.startswith("alsa_card."):
        return None
    params = (obj.get("info") or {}).get("params") or {}
    profiles = params.get("EnumProfile", [])
    current = params.get("Profile", [])
    off = next((p["index"] for p in profiles if p.get("name") == "off"), None)
    non_off = max(
        (p for p in profiles if p.get("name") != "off"),
        key=lambda p: p.get("priority", 0),
        default=None,
    )
    return Device(
        id=obj["id"],
        name=name,
        description=props.get("device.description", name),
        active_profile_index=current[0].get("index") if current else None,
        off_profile_index=off,
        restore_profile_index=non_off["index"] if non_off else None,
    )


@dataclass
class Graph:
    """One `pw-dump`'s worth of graph state: the Audio nodes and the ALSA
    card devices, which a single dump already carries together.

    Passed around explicitly rather than re-fetched, because a dump is
    expensive and there is no cheaper query: 85ms to run plus 17ms to parse
    ~495KB of JSON on sagepi's Pi 4. An earlier version had separate
    list_nodes()/list_devices() calls, which meant the enable/disable path
    alone paid for three dumps - and, far worse, the pw-mon watcher paid for
    one *per changed object id*. A single headset profile flip emits 147
    changed events across 72 distinct ids, so that was ~7 seconds of dump
    work for one click (measured live; the click itself took 9.7s).

    Deliberately carries no volume/mute: that needs a separate
    `wpctl get-volume` per node (see get_volume_mute), and callers should
    only pay it for the handful of nodes they actually display, not for
    every node in the graph.
    """

    nodes: list[Node]
    devices: list[Device]


def dump() -> Graph:
    objects = json.loads(_run(["pw-dump"]))
    nodes: list[Node] = []
    devices: list[Device] = []
    for obj in objects:
        obj_type = obj.get("type")
        if obj_type == "PipeWire:Interface:Node":
            node = _parse_node(obj)
            if node is not None:
                nodes.append(node)
        elif obj_type == "PipeWire:Interface:Device":
            device = _parse_device(obj)
            if device is not None:
                devices.append(device)
    return Graph(nodes=nodes, devices=devices)


def set_device_profile(device_id: int, profile_index: int) -> None:
    """A profile flip tears down and recreates the card's nodes, so no
    cached volume can be assumed to still belong to the node it was read
    from."""
    try:
        _run(["wpctl", "set-profile", str(device_id), str(profile_index)])
    finally:
        forget_volumes()


def find_node_id(nodes: list[Node], name: str, media_class_prefix: str | None = None) -> int | None:
    for node in nodes:
        if node.name == name:
            if media_class_prefix and not node.media_class.startswith(media_class_prefix):
                continue
            return node.id
    return None


def set_volume(node_id: int, volume: float) -> None:
    volume = max(0.0, min(1.5, volume))
    try:
        _run(["wpctl", "set-volume", str(node_id), f"{volume:.2f}"])
    finally:
        forget_volume(node_id)


def set_channel_volumes(node_id: int, volume: float, balance: float) -> None:
    """Apply volume + left/right balance together as raw stereo channel
    volumes, since that's the only way PipeWire exposes panning - there's
    no separate "balance" knob to set. balance is -1.0 (full left) to 1.0
    (full right), 0.0 = center/flat (in which case this matches set_volume
    exactly). Cubed to match wpctl's cubic display scale (see
    get_volume_mute) so a centered balance doesn't silently shift the
    perceived loudness that `set_volume` alone would have produced.

    Only meaningful for genuinely stereo nodes - a mono node has one
    channel and pw-cli will reject a 2-element channelVolumes array for it.
    """
    volume = max(0.0, min(1.5, volume))
    balance = max(-1.0, min(1.0, balance))
    left = volume * min(1.0, 1.0 - balance)
    right = volume * min(1.0, 1.0 + balance)
    props = json.dumps({"channelVolumes": [left**3, right**3]})
    try:
        _run(["pw-cli", "set-param", str(node_id), "Props", props])
    finally:
        forget_volume(node_id)


def set_mute(node_id: int, muted: bool) -> None:
    try:
        _run(["wpctl", "set-mute", str(node_id), "1" if muted else "0"])
    finally:
        forget_volume(node_id)


def load_module(name: str, args: dict) -> int:
    """Hot-load a module into the live daemon, returns the new module id."""
    try:
        out = _run(["pw-cli", "load-module", name, json.dumps(args)])
    finally:
        forget_volumes()  # new nodes can take ids the cache has readings for
    m = re.search(r"id:\s*(\d+)", out) or re.search(r"^(\d+)", out.strip())
    if not m:
        raise PipewireError(f"could not parse module id from: {out!r}")
    return int(m.group(1))


def unload_module(module_id: int) -> None:
    try:
        _run(["pw-cli", "destroy", str(module_id)])
    finally:
        forget_volumes()
