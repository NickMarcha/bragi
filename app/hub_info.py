"""Where this hub is, for the tray apps (bragi#61).

A tray app kept the hub's address in up to four places: its server URL,
the Tailscale IP its audio goes to (BRAGI_HOST_IP), a Linux Roc link's env
file, and the Deskflow client's remoteHost. Moving the hub meant editing
all of them by hand on every computer.

Now the hub says where it is. Each tray app gets a "hub" message when it
connects: the hub's wss:// URL, Tailscale IP, LAN IP and the addresses its
Deskflow clients should use. The tray app fixes whatever differs. To move
to another hub, the dashboard sends "hub_moved" with the new URL; the tray
apps switch, connect there, and the new hub's "hub" message does the rest.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import json
import os
import re
import shutil
import socket
import subprocess
import time

logger = logging.getLogger("bragi.hub_info")
CACHE_SECONDS = 60
_cache: tuple[float, dict] | None = None
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$")


def _tailscale() -> dict:
    """This node's Tailscale name and IPv4, or {} without Tailscale."""
    if not shutil.which("tailscale"):
        return {}
    try:
        res = subprocess.run(["tailscale", "status", "--self", "--peers=false", "--json"],
                             capture_output=True, text=True, timeout=5)
        me = json.loads(res.stdout or "{}").get("Self") or {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    ipv4 = next((ip for ip in me.get("TailscaleIPs") or [] if ":" not in ip), None)
    return {"name": (me.get("DNSName") or "").rstrip(".") or None, "ip": ipv4}


def lan_ip() -> str | None:
    """The address of the interface with the default route (no packet is sent).
    BRAGI_LAN_IP overrides it."""
    if os.environ.get("BRAGI_LAN_IP"):
        return os.environ["BRAGI_LAN_IP"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed anywhere real
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ipaddress.ip_address(ip).is_loopback or ip.startswith("100.") else ip


def _kvm_installed() -> bool:
    return bool(shutil.which("deskflow-core"))


def message() -> dict:
    """The "hub" message a tray app gets in answer to its client_hello. From
    the cache refresh() keeps warm; computed here only before the first refresh."""
    if _cache is not None:
        return _cache[1]
    return _compute()


def _compute() -> dict:
    global _cache
    now = time.monotonic()
    ts = _tailscale()
    lan = lan_ip()
    msg = {
        "type": "hub",
        "ws_url": f"wss://{ts['name']}/ws/peer" if ts.get("name") else None,
        "tailscale_ip": ts.get("ip"),
        "lan_ip": lan,
        # LAN first: Deskflow tries them in order.
        "deskflow_hosts": [h for h in (lan, ts.get("ip")) if h] if _kvm_installed() else [],
    }
    _cache = (now, msg)
    return msg


def forget() -> None:
    global _cache
    _cache = None


async def refresh() -> None:
    """Runs forever as a lifespan task: keeps message()'s cache current, off
    the event loop (tailscale status is a subprocess)."""
    while True:
        try:
            await asyncio.to_thread(_compute)
        except Exception:
            logger.exception("hub info refresh failed")
        await asyncio.sleep(CACHE_SECONDS)


def ws_url(text: str) -> str:
    """The peer-socket URL for what someone typed: a host name, an https://
    dashboard URL or a wss://…/ws/peer URL. Raises ValueError."""
    text = (text or "").strip()
    host = re.sub(r"^(https?|wss?)://", "", text).split("/", 1)[0]
    if ":" in host:
        host, port = host.rsplit(":", 1)
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError("That port isn't a number.")
        host_port = f"{host}:{port}"
    else:
        host_port = host
    if not host or not _HOST.match(host):
        raise ValueError("Type the new hub's address, e.g. sagehub.tailnet.ts.net.")
    return f"wss://{host_port}/ws/peer"
