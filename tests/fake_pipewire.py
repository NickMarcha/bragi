"""A fake PipeWire host, substituted at `pipewire._run` - the one place the
app shells out (see docs/development.md: "app/pipewire.py is the seam to
mock").

Faking at the subprocess boundary rather than at `pipewire.dump()` is
deliberate: subprocess *count* is the thing that actually made the dashboard
slow. A `pw-dump` costs ~100ms on sagepi's Pi 4 (85ms to run, 17ms to parse
495KB of JSON) and a `wpctl get-volume` ~50ms, so "how many times did we
shell out" is the real performance contract, and it is the one thing a test
can assert exactly. Every call lands in `calls`; the perf tests assert on
`count()`.

Card enable/disable is modelled faithfully, because it is load-bearing:
flipping an ALSA card profile to "off" makes its Sink/Source nodes vanish
from the graph entirely, and that is exactly the state the enable/disable
path has to keep working in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app import pipewire

OFF_PROFILE = 0
STEREO_PROFILE = 1


@dataclass
class FakeNode:
    id: int
    name: str
    description: str
    media_class: str
    volume: float = 1.0
    muted: bool = False
    channel_volumes: list[float] | None = None


@dataclass
class FakeCard:
    """An ALSA card Device, plus the nodes that exist only while it is on."""

    id: int
    name: str  # "alsa_card.usb-..."
    description: str
    nodes: list[FakeNode] = field(default_factory=list)
    active_index: int = STEREO_PROFILE
    profiles: list[tuple[int, str, int]] = field(
        default_factory=lambda: [
            (OFF_PROFILE, "off", 0),
            (STEREO_PROFILE, "output:analog-stereo+input:mono-fallback", 6501),
            (2, "output:analog-stereo", 6500),
        ]
    )

    @property
    def enabled(self) -> bool:
        return self.active_index != OFF_PROFILE


class FakeSession:
    def __init__(self, nodes: list[FakeNode] | None = None, cards: list[FakeCard] | None = None):
        self.free_nodes = list(nodes or [])
        self.cards = list(cards or [])
        self.calls: list[list[str]] = []

    # --- graph state -----------------------------------------------------

    def live_nodes(self) -> list[FakeNode]:
        nodes = list(self.free_nodes)
        for card in self.cards:
            if card.enabled:
                nodes.extend(card.nodes)
        return nodes

    def node(self, node_id: int) -> FakeNode | None:
        return next((n for n in self.live_nodes() if n.id == node_id), None)

    def card(self, card_id: int) -> FakeCard | None:
        return next((c for c in self.cards if c.id == card_id), None)

    # --- call accounting -------------------------------------------------

    def count(self, *prefix: str) -> int:
        """How many recorded calls start with these argv elements."""
        return sum(1 for c in self.calls if c[: len(prefix)] == list(prefix))

    def reset_calls(self) -> None:
        self.calls.clear()

    # --- the _run replacement --------------------------------------------

    def run(self, args: list[str], input_text: str | None = None) -> str:
        self.calls.append(list(args))
        head = args[0]
        if head == "pw-dump":
            return self._pw_dump()
        if head == "wpctl":
            return self._wpctl(args[1:])
        if head == "pw-cli":
            return self._pw_cli(args[1:])
        raise pipewire.PipewireError(f"fake has no handler for {args!r}")

    def _pw_dump(self) -> str:
        objects: list[dict] = []
        for node in self.live_nodes():
            objects.append(
                {
                    "id": node.id,
                    "type": "PipeWire:Interface:Node",
                    "info": {
                        "props": {
                            "node.name": node.name,
                            "node.description": node.description,
                            "media.class": node.media_class,
                        },
                        "params": {},
                    },
                }
            )
        for card in self.cards:
            objects.append(
                {
                    "id": card.id,
                    "type": "PipeWire:Interface:Device",
                    "info": {
                        "props": {
                            "device.name": card.name,
                            "device.description": card.description,
                            "media.class": "Audio/Device",
                        },
                        "params": {
                            "EnumProfile": [
                                {"index": i, "name": n, "description": n, "priority": p}
                                for i, n, p in card.profiles
                            ],
                            "Profile": [{"index": card.active_index}],
                        },
                    },
                }
            )
        # Filler of the kind a real dump carries alongside (a live sagepi dump
        # is 167 objects, only 24 of them Nodes), so type filtering is
        # exercised the way it is in production.
        for i in range(300, 340):
            objects.append({"id": i, "type": "PipeWire:Interface:Port", "info": {"props": {}}})
        return json.dumps(objects)

    def _wpctl(self, args: list[str]) -> str:
        verb = args[0]
        if verb == "set-profile":
            card = self.card(int(args[1]))
            if card is None:
                raise pipewire.PipewireError(f"Device {args[1]} not found")
            card.active_index = int(args[2])
            return ""
        node = self.node(int(args[1])) if len(args) > 1 else None
        if node is None:
            raise pipewire.PipewireError(f"Node {args[1]} not found")
        if verb == "get-volume":
            return f"Volume: {node.volume:.2f}" + (" [MUTED]" if node.muted else "") + "\n"
        if verb == "set-volume":
            node.volume = float(args[2])
            return ""
        if verb == "set-mute":
            node.muted = args[2] == "1"
            return ""
        raise pipewire.PipewireError(f"fake has no handler for wpctl {verb}")

    def _pw_cli(self, args: list[str]) -> str:
        if args[0] == "set-param":
            node = self.node(int(args[1]))
            if node is None:
                raise pipewire.PipewireError(f"Node {args[1]} not found")
            node.channel_volumes = json.loads(args[3])["channelVolumes"]
            return ""
        if args[0] == "load-module":
            return "id: 999\n"
        if args[0] == "destroy":
            return ""
        raise pipewire.PipewireError(f"fake has no handler for pw-cli {args[0]}")


HEADSET_CARD_ID = "usb-HP__Inc_HyperX_Cloud_III_S_Wireless_C1V5340J99-00"
SECOND_CARD_ID = "usb-HP__Inc_HyperX_Cloud_III_Wireless_0000000000000000-00"


def sagepi_session() -> FakeSession:
    """The graph as it actually stands on sagepi, captured from a live
    `pw-dump`: three peers, one enabled headset, one disabled headset."""
    peer_nodes = [
        FakeNode(50, "sagedeck-test-sink", "sagedeck-test-sink", "Audio/Sink"),
        FakeNode(53, "sagedeck-audio", "sagedeck-audio", "Stream/Output/Audio"),
        FakeNode(56, "sagedev-test-sink", "sagedev-test-sink", "Audio/Sink"),
        FakeNode(58, "sagedev-audio", "sagedev-audio", "Stream/Output/Audio"),
        FakeNode(211, "vban", "vban", "Stream/Output/Audio"),
        FakeNode(220, "vban", "vban", "Stream/Input/Audio"),
        FakeNode(117, "alsa_output.platform-fe00b840.mailbox.stereo-fallback", "Built-in Audio", "Audio/Sink"),
    ]
    enabled = FakeCard(
        id=90,
        name=f"alsa_card.{HEADSET_CARD_ID}",
        description="HyperX Cloud III S Wireless",
        nodes=[
            FakeNode(
                233,
                f"alsa_output.{HEADSET_CARD_ID}.analog-stereo",
                "HyperX Cloud III S Wireless Analog Stereo",
                "Audio/Sink",
            ),
            FakeNode(
                124,
                f"alsa_input.{HEADSET_CARD_ID}.mono-fallback",
                "HyperX Cloud III S Wireless Mono",
                "Audio/Source",
            ),
        ],
    )
    disabled = FakeCard(
        id=91,
        name=f"alsa_card.{SECOND_CARD_ID}",
        description="HyperX Cloud III Wireless",
        active_index=OFF_PROFILE,
        nodes=[
            FakeNode(
                250,
                f"alsa_output.{SECOND_CARD_ID}.analog-stereo",
                "HyperX Cloud III Wireless Analog Stereo",
                "Audio/Sink",
            ),
            FakeNode(
                251,
                f"alsa_input.{SECOND_CARD_ID}.mono-fallback",
                "HyperX Cloud III Wireless Mono",
                "Audio/Source",
            ),
        ],
    )
    return FakeSession(nodes=peer_nodes, cards=[enabled, disabled])
