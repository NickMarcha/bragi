"""Android peer configuration and commands, separate from dashboard fader timing.

Android clients opt in with ?client=android and receive configuration and
stream commands. Desktop tray apps receive the gamepad status and, on the
computer that has the controllers, their input (gamepad.py); they can send
gamepad_claim and gamepad_release. Older desktop clients read and ignore
whatever arrives.
Each connection has one writer; dashboard actions only enqueue commands.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
from dataclasses import asdict

from fastapi import WebSocket, WebSocketDisconnect

from . import gamepad, hub_info, kvm, monitors, peer_presence, peers, ws

logger = logging.getLogger("bragi.peer_control")

_connections: dict[str, dict[WebSocket, asyncio.Queue]] = {}
# Sockets of Android clients; every other connection is a desktop tray app.
_android: set[WebSocket] = set()
# A droppable message (a gamepad state) is skipped once this many are queued:
# the next one carries the whole state anyway.
_DROP_AFTER = 8
statuses: dict[str, dict] = {}
# The tailnet address each phone's control socket actually came from.
seen_addresses: dict[str, str] = {}


def config(peer: peers.Peer) -> dict:
    return {
        'name': peer.name, 'ports': asdict(peer.ports), 'fec': 'disable',
        'sample_rate': 44100, 'channels': 2, 'capture_mode': peer.capture_mode,
        'send_enabled': peer.send_enabled, 'receive_enabled': peer.receive_enabled,
    }


def _delay_ms(value) -> int | None:
    """A delay the phone measured, or None when it is missing or implausible."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 5000:
        return value
    return None


def _tailnet_source(socket: WebSocket) -> str | None:
    """The phone's tailnet address as Tailscale Serve forwards it, or None.

    Bragi only listens on loopback behind `tailscale serve`, so the header
    comes from Tailscale, not from the phone.
    """
    forwarded = socket.headers.get('x-forwarded-for', '').split(',')[0].strip()
    try:
        address = ipaddress.ip_address(forwarded)
    except ValueError:
        return None
    return forwarded if address in ipaddress.ip_network('100.64.0.0/10') else None


def address_warning(peer: peers.Peer) -> str | None:
    """Says so when audio goes to an address the phone is not at (issue #2)."""
    seen = seen_addresses.get(peer.name)
    if seen is None or seen == peer.tailscale_ip:
        return None
    return f'Phone is at {seen}, Bragi sends to {peer.tailscale_ip}. Reopen the app.'


def publish_streams(peer: peers.Peer) -> dict:
    message = {'type': 'peer_streams', 'name': peer.name,
               'send_enabled': peer.send_enabled, 'receive_enabled': peer.receive_enabled}
    for queue in _connections.get(peer.name, {}).values():
        queue.put_nowait(message)
    ws.manager.broadcast_nowait(message)
    return message


def send(name: str, message: dict, droppable: bool = False) -> None:
    """Queues a message for every desktop connection of this peer."""
    for socket, queue in _connections.get(name, {}).items():
        if socket in _android or (droppable and queue.qsize() >= _DROP_AFTER):
            continue
        queue.put_nowait(message)


def broadcast_desktop(message: dict) -> None:
    """Queues a message for every desktop tray app, and for the dashboard."""
    for connections in _connections.values():
        for socket, queue in connections.items():
            if socket not in _android:
                queue.put_nowait(message)
    ws.manager.broadcast_nowait(message)


# What each connected tray app can do besides audio, from its client_hello:
# "keys" = it presses keys on its own computer (press_keys).
_features: dict[str, set[str]] = {}


def has_feature(peer: str, feature: str) -> bool:
    return feature in _features.get(peer, set()) and peer in _connections


def peer_for_computer(computer: str, computers: list[dict]) -> str | None:
    """The connected tray app on a KVM computer: one whose peer name is the
    computer's name or one of its aliases."""
    entry = next((c for c in computers if c["name"] == computer), {"name": computer, "aliases": []})
    names = {n.lower() for n in [entry["name"], *entry.get("aliases", [])]}
    return next((p for p, conns in _connections.items()
                 if p.lower() in names and any(s not in _android for s in conns)), None)


def desktop_peers() -> set[str]:
    """The peers with a tray app connected now (not the Android app)."""
    return {p for p, conns in _connections.items() if any(s not in _android for s in conns)}


def press_keys(peer: str, codes: list[int]) -> None:
    """Asks a tray app to press and release these evdev key codes, in
    order, on its own computer."""
    send(peer, {"type": "press_keys", "codes": codes})


def identify(computers: list[dict]) -> dict:
    """Asks every connected tray app to show its computer's name on its
    screens for a few seconds, like Windows' "Identify" in display settings.
    A tray app is matched to a KVM computer by its peer name, or one of the
    computer's aliases. Returns who was asked and which computers have no
    tray app connected to show it."""
    def computer_for(peer: str) -> str | None:
        for c in computers:
            if peer.lower() in (n.lower() for n in [c["name"], *c.get("aliases", [])]):
                return c["name"]
        return None

    shown: dict[str, str | None] = {}
    for peer, connections in _connections.items():
        if any(socket not in _android for socket in connections):
            shown[peer] = computer_for(peer)
            send(peer, {"type": "kvm_identify", "computer": shown[peer], "peer": peer})
    reached = {c for c in shown.values() if c}
    return {"shown": shown, "missing": [c["name"] for c in computers if c["name"] not in reached]}


def _desktop_message(name: str, raw: str) -> None:
    """What a desktop tray app may ask: take or give back the controllers."""
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(message, dict):
        return
    if message.get('type') == 'kvm_switch':
        _kvm_switch(name, raw)
        return
    if message.get('type') == 'client_hello' and isinstance(message.get('features'), list):
        _features[name] = {f for f in message['features'] if f in ('keys',)}
        # Where this hub is, so the tray app can fix stale addresses (bragi#61).
        # From the cache hub_info.refresh() keeps warm, so this never blocks.
        send(name, hub_info.message())
        return
    if message.get('type') == 'monitors':
        # This computer's monitors, for the KVM layout (app/monitors.py).
        try:
            monitors.report(name, message)
        except monitors.MonitorError as exc:
            logger.info("monitors from %s refused: %s", name, exc)
        return
    if gamepad.hub is None:
        return
    kind = message.get('type')
    if kind == 'gamepad_hello' and isinstance(message.get('modes'), list):
        gamepad.hub.hello(name, message['modes'])
    elif kind == 'gamepad_claim':
        gamepad.hub.set_target(name)
    elif kind == 'gamepad_release':
        gamepad.hub.release(name)
    elif kind in ('hid_output', 'hid_get_report', 'hid_set_report'):
        gamepad.hub.hid_request(name, message)
    elif kind == 'gamepad_rumble':
        gamepad.hub.rumble(name, message)


def _kvm_switch(name: str, raw: str) -> None:
    """A tray app's "Keyboard & mouse" menu: switch to a computer. The
    answer comes back as the next "kvm" broadcast (or an error message)."""
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(message, dict) or message.get('type') != 'kvm_switch':
        return
    computer = message.get('computer')
    if not isinstance(computer, str) or len(computer) > 64:
        return

    async def run() -> None:
        try:
            await asyncio.to_thread(kvm.switch, computer)
        except kvm.KvmError as exc:
            send(name, {'type': 'kvm_error', 'error': str(exc)})

    asyncio.get_running_loop().create_task(run())


def move_clients(url: str) -> int:
    """Tells every connected tray app and phone to switch to another hub
    (bragi#61, phones since bragi#67). Returns how many were told. Raises
    ValueError for a bad URL."""
    message = {'type': 'hub_moved', 'ws_url': hub_info.ws_url(url)}
    count = 0
    for connections in _connections.values():
        for queue in connections.values():
            queue.put_nowait(message)
            count += 1
    return count


def remove(name: str) -> None:
    for queue in _connections.get(name, {}).values():
        queue.put_nowait({'type': 'peer_removed', 'name': name})


async def endpoint(socket: WebSocket, name: str) -> None:
    android = socket.query_params.get('client') == 'android'
    peer = next((p for p in peers.load_peers() if p.name == name), None) if android else None
    if android and (peer is None or peer.client_kind != 'android'):
        await socket.close(code=1008, reason='Register this Android peer first')
        return
    await socket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    connections = _connections.setdefault(name, {})
    connections[socket] = queue
    if android:
        _android.add(socket)
    else:
        if gamepad.hub is not None:
            queue.put_nowait(gamepad.hub.status())
        # The tray's "Keyboard & mouse" menu starts from this.
        queue.put_nowait({'type': 'kvm', **kvm.read_view()})
    peer_presence.connected_peers.add(name)
    if android and (seen := _tailnet_source(socket)):
        seen_addresses[name] = seen
    ws.manager.broadcast_nowait({'type': 'peer_presence', 'name': name, 'connected': True})
    receive_task = queue_task = None
    try:
        if android:
            await socket.send_json({'type': 'peer_config', **config(peer)})
        while True:
            if receive_task is None:
                receive_task = asyncio.create_task(socket.receive_text())
            if queue_task is None:
                queue_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({receive_task, queue_task}, return_when=asyncio.FIRST_COMPLETED)
            if receive_task in done:
                raw = receive_task.result()
                receive_task = None
                if not android:
                    _desktop_message(name, raw)
                if android:
                    try:
                        message = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(message, dict) and message.get('type') == 'peer_status':
                        status = {'type': 'peer_status', 'name': name,
                                  'send_active': message.get('send_active') is True,
                                  'receive_active': message.get('receive_active') is True,
                                  'error': str(message['error'])[:300] if message.get('error') else None,
                                  'send_delay_ms': _delay_ms(message.get('send_delay_ms')),
                                  'listen_delay_ms': _delay_ms(message.get('listen_delay_ms')),
                                  # Re-read: the phone may have registered a new address since.
                                  'address_warning': address_warning(next(
                                      (p for p in peers.load_peers() if p.name == name), peer))}
                        statuses[name] = status
                        ws.manager.broadcast_nowait(status)
                        queue.put_nowait(status)
            if queue_task in done:
                message = queue_task.result()
                queue_task = None
                await socket.send_json(message)
                if message.get('type') == 'peer_removed':
                    await socket.close(code=1008, reason='Peer removed from Bragi')
                    break
    except WebSocketDisconnect:
        pass
    finally:
        connections.pop(socket, None)
        _android.discard(socket)
        if not connections:
            _connections.pop(name, None)
            statuses.pop(name, None)
            seen_addresses.pop(name, None)
            peer_presence.connected_peers.discard(name)
            _features.pop(name, None)
            if gamepad.hub is not None:
                gamepad.hub.forget(name)
            ws.manager.broadcast_nowait({'type': 'peer_presence', 'name': name, 'connected': False})
        for task in (receive_task, queue_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task
