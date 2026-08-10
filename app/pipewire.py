"""Thin subprocess wrapper around pw-dump / wpctl / pw-cli.

Bragi never talks to the PipeWire socket directly - it shells out to the
same CLI tools a human would use, on purpose. That keeps this module small
and lets `wpctl`/`pw-cli` (which already know how to encode Props params
correctly) do the hard part.
"""

from __future__ import annotations

import json
import re
import subprocess
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
    volume: float | None = None
    muted: bool = False


def list_nodes() -> list[Node]:
    """All Audio nodes currently in the PipeWire graph, with live volume."""
    raw = _run(["pw-dump"])
    objects = json.loads(raw)
    nodes: list[Node] = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        media_class = props.get("media.class", "")
        if not media_class.startswith("Audio/") and not media_class.startswith("Stream/"):
            continue
        if "Audio" not in media_class:
            continue
        node_id = obj["id"]
        volume, muted = _read_volume(node_id)
        nodes.append(
            Node(
                id=node_id,
                name=props.get("node.name", f"node-{node_id}"),
                description=props.get("node.description", props.get("node.name", "")),
                media_class=media_class,
                volume=volume,
                muted=muted,
            )
        )
    return nodes


_VOLUME_RE = re.compile(r"Volume:\s*([0-9.]+)\s*(\[MUTED\])?")


def _read_volume(node_id: int) -> tuple[float | None, bool]:
    try:
        out = _run(["wpctl", "get-volume", str(node_id)])
    except PipewireError:
        return None, False
    m = _VOLUME_RE.search(out)
    if not m:
        return None, False
    return float(m.group(1)), bool(m.group(2))


def find_node_id(name: str, media_class_prefix: str | None = None) -> int | None:
    for node in list_nodes():
        if node.name == name:
            if media_class_prefix and not node.media_class.startswith(media_class_prefix):
                continue
            return node.id
    return None


def set_volume(node_id: int, volume: float) -> None:
    volume = max(0.0, min(1.5, volume))
    _run(["wpctl", "set-volume", str(node_id), f"{volume:.2f}"])


def set_mute(node_id: int, muted: bool) -> None:
    _run(["wpctl", "set-mute", str(node_id), "1" if muted else "0"])


def load_module(name: str, args: dict) -> int:
    """Hot-load a module into the live daemon, returns the new module id."""
    out = _run(["pw-cli", "load-module", name, json.dumps(args)])
    m = re.search(r"id:\s*(\d+)", out) or re.search(r"^(\d+)", out.strip())
    if not m:
        raise PipewireError(f"could not parse module id from: {out!r}")
    return int(m.group(1))


def unload_module(module_id: int) -> None:
    _run(["pw-cli", "destroy", str(module_id)])
