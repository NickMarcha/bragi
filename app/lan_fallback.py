"""Audio over the home network while this hub's Tailscale is down (bragi#81).

Every peer's audio goes to its Tailscale address. On 2026-11-21 the
internet was down when the hub rebooted: tailscaled couldn't log in, so the
hub had no 100.x address and every audio link was dead, while the KVM kept
working over the LAN. Tray apps now report their LAN address when they
connect (peers.set_lan_ip), and this supervisor switches peers.conf to
those addresses while the hub has no Tailscale address of its own, and back
when it has one again.

The test is deliberately narrow: no IPv4 address in 100.64.0.0/10 on any
interface, read from /proc/net/fib_trie (no subprocess, no root). That is
the failure seen. Peers whose own Tailscale is down, or a hub whose
Tailscale is up but can't reach anyone, change nothing, so a handheld away
from home is never sent to a home address. It switches to the LAN only
after DOWN_CHECKS checks in a row, so tailscaled coming up a few seconds
after Bragi at boot doesn't restart the peers twice; it switches back at
the first check that finds an address.

Writing peers.conf is the whole switch: bragi-peers.path restarts the
peers once when the file changes, and an unchanged file isn't written.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from pathlib import Path

from . import peers

logger = logging.getLogger("bragi.lan_fallback")

INTERVAL = 5.0
DOWN_CHECKS = 3
FIB_TRIE = Path("/proc/net/fib_trie")
_TAILNET = ipaddress.ip_network("100.64.0.0/10")


def local_ipv4(fib_trie: str) -> set[str]:
    """This machine's own IPv4 addresses, from /proc/net/fib_trie: each
    address line is followed by its "/32 host LOCAL" line."""
    found: set[str] = set()
    last = None
    for line in fib_trie.splitlines():
        text = line.strip()
        if text.startswith("|--"):
            last = text[3:].strip()
        elif last and text.startswith("/32 host LOCAL"):
            found.add(last)
    return found


def tailscale_up(path: Path | None = None) -> bool | None:
    """Whether any interface has a Tailscale IPv4 address; None when it
    can't be told (no /proc/net/fib_trie: not Linux)."""
    try:
        text = (path or FIB_TRIE).read_text()
    except OSError:
        return None
    addresses = set()
    for address in local_ipv4(text):
        try:
            addresses.add(ipaddress.ip_address(address))
        except ValueError:
            continue
    return any(a.version == 4 and a in _TAILNET for a in addresses)


class Switch:
    """Whether the peers should use LAN addresses, from one check after
    another."""

    def __init__(self, down_checks: int = DOWN_CHECKS):
        self.down_checks = down_checks
        self.down = 0
        self.use_lan = False

    def update(self, up: bool | None) -> bool:
        if up is None:
            return self.use_lan
        if up:
            self.down = 0
            self.use_lan = False
        else:
            self.down += 1
            if self.down >= self.down_checks:
                self.use_lan = True
        return self.use_lan


def active() -> bool:
    """True while peers.conf sends to LAN addresses, for Settings > This hub."""
    return peers.using_lan()


async def supervise() -> None:
    """Runs forever as a lifespan task."""
    switch = Switch()
    while True:
        try:
            want = switch.update(await asyncio.to_thread(tailscale_up))
            if want != peers.using_lan():
                if await asyncio.to_thread(peers.set_lan_mode, want):
                    logger.warning("this hub's Tailscale is %s: peers use their %s addresses",
                                   "down" if want else "back", "LAN" if want else "Tailscale")
        except Exception:
            logger.exception("LAN fallback check failed")
        await asyncio.sleep(INTERVAL)
