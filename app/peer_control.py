"""Android peer configuration and commands, separate from dashboard fader timing.

Old desktop clients keep their receive-free presence socket. Android clients
opt in with ?client=android and receive configuration and stream commands.
Each connection has one writer; dashboard actions only enqueue commands.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import asdict

from fastapi import WebSocket, WebSocketDisconnect

from . import peer_presence, peers, ws

_connections: dict[str, dict[WebSocket, asyncio.Queue]] = {}
statuses: dict[str, dict] = {}


def config(peer: peers.Peer) -> dict:
    return {
        'name': peer.name, 'ports': asdict(peer.ports), 'fec': 'disable',
        'sample_rate': 44100, 'channels': 2, 'capture_mode': peer.capture_mode,
        'send_enabled': peer.send_enabled, 'receive_enabled': peer.receive_enabled,
    }


def publish_streams(peer: peers.Peer) -> dict:
    message = {'type': 'peer_streams', 'name': peer.name,
               'send_enabled': peer.send_enabled, 'receive_enabled': peer.receive_enabled}
    for queue in _connections.get(peer.name, {}).values():
        queue.put_nowait(message)
    ws.manager.broadcast_nowait(message)
    return message


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
    peer_presence.connected_peers.add(name)
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
                if android:
                    try:
                        message = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(message, dict) and message.get('type') == 'peer_status':
                        status = {'type': 'peer_status', 'name': name,
                                  'send_active': message.get('send_active') is True,
                                  'receive_active': message.get('receive_active') is True,
                                  'error': str(message['error'])[:300] if message.get('error') else None}
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
        if not connections:
            _connections.pop(name, None)
            statuses.pop(name, None)
            peer_presence.connected_peers.discard(name)
            ws.manager.broadcast_nowait({'type': 'peer_presence', 'name': name, 'connected': False})
        for task in (receive_task, queue_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task
