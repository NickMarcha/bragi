"""Detects sagepi's own USB headset(s) - the physical devices, not network
peers - and pairs each one's playback (Audio/Sink) and capture
(Audio/Source) PipeWire nodes by ALSA card id, so the UI can show one card
per physical headset instead of two unrelated-looking rows.

Deliberately has no persisted registry like peers.py: whatever's plugged
in *is* the list, there's nothing to add/remove or survive a restart.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import pipewire


@dataclass
class Headset:
    key: str
    label: str
    playback_node_id: int | None
    playback_node_name: str | None
    capture_node_id: int | None
    capture_node_name: str | None


def _card_id(node_name: str, prefix: str) -> str | None:
    """'alsa_output.usb-...-00.analog-stereo' -> 'usb-...-00' (drops the
    profile suffix so the same physical card's sink and source pair up).
    Only matches USB ALSA cards ('usb-' after the prefix) - sagepi also has
    a 'Built-in Audio' analog jack (node name 'alsa_output.platform-...')
    that isn't a headset and shouldn't show up here."""
    if not node_name.startswith(prefix):
        return None
    rest = node_name[len(prefix):]
    if not rest.startswith("usb-"):
        return None
    return rest.rsplit(".", 1)[0] if "." in rest else rest


def list_headsets(nodes: list[pipewire.Node]) -> list[Headset]:
    playback: dict[str, pipewire.Node] = {}
    capture: dict[str, pipewire.Node] = {}
    for node in nodes:
        if node.media_class == "Audio/Sink":
            card = _card_id(node.name, "alsa_output.")
            if card:
                playback[card] = node
        elif node.media_class == "Audio/Source":
            card = _card_id(node.name, "alsa_input.")
            if card:
                capture[card] = node

    headsets = []
    for card in sorted(set(playback) | set(capture)):
        out_node = playback.get(card)
        in_node = capture.get(card)
        label = (out_node or in_node).description or card
        headsets.append(
            Headset(
                key=card,
                label=label,
                playback_node_id=out_node.id if out_node else None,
                playback_node_name=out_node.name if out_node else None,
                capture_node_id=in_node.id if in_node else None,
                capture_node_name=in_node.name if in_node else None,
            )
        )
    return headsets
