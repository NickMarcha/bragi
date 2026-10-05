"""Choose the microphone feeding desktop peers and restore its links after reconnects.

Desktop Roc and VBAN sinks remain unchanged. Only their input
links change. Android playback receivers are excluded so microphone audio is not
mixed into the phone's headset playback feed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

from . import peers, pipewire

logger = logging.getLogger('bragi.microphone')
_lock = threading.RLock()


def selected() -> str:
    path = peers.DATA_DIR / 'microphone.json'
    return json.loads(path.read_text())['source'] if path.exists() else 'headset'


def options() -> list[dict]:
    return [{'key': 'headset', 'label': 'Pi headset microphone'}] + [
        {'key': p.name, 'label': f'{p.name} microphone'} for p in peers.load_peers()
        if p.client_kind == 'android' and p.capture_mode == 'microphone'
    ]


def view() -> dict:
    source = selected()
    choices = options()
    if not any(choice['key'] == source for choice in choices):
        choices.append({'key': source, 'label': f'{source} microphone (unavailable)'})
    return {'source': source, 'options': choices}


def select(source: str) -> None:
    if not any(choice['key'] == source for choice in options()):
        raise ValueError('Choose the Pi headset or a registered phone in microphone mode')
    peers.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        path = peers.DATA_DIR / 'microphone.json'
        pending = path.with_suffix('.tmp')
        pending.write_text(json.dumps({'source': source}))
        pending.replace(path)
        reconcile()


def reconcile() -> None:
    with _lock:
        _reconcile()


def _reconcile() -> None:
    # Preserve the existing host links until the user first selects a microphone.
    if not (peers.DATA_DIR / 'microphone.json').exists():
        return
    registry = peers.load_peers()
    objects = json.loads(pipewire._run(['pw-dump']))
    nodes = {obj['id']: obj.get('info', {}).get('props', {}) for obj in objects
             if obj.get('type') == 'PipeWire:Interface:Node'}
    source = selected()
    phone = next((p for p in registry if p.name == source and p.client_kind == 'android'
                  and p.capture_mode == 'microphone'), None)
    source_id = next((node_id for node_id, props in nodes.items()
                      if props.get('media.class') == 'Audio/Source' and (
                          props.get('node.name', '').startswith('alsa_input.usb-') if source == 'headset'
                          else phone is not None and props.get('node.name') == phone.incoming_source_name)), None)
    sink_names = {p.outgoing_sink_name for p in registry if p.client_kind != 'android'}
    target_ids = {node_id for node_id, props in nodes.items()
                  if props.get('node.name') in sink_names}
    ports = [(obj['id'], obj.get('info', {}).get('props', {})) for obj in objects
             if obj.get('type') == 'PipeWire:Interface:Port']
    outputs = [(port_id, props) for port_id, props in ports
               if str(props.get('node.id')) == str(source_id) and props.get('port.direction') == 'out']
    inputs = [(port_id, props) for port_id, props in ports
              if str(props.get('node.id')) in {str(i) for i in target_ids} and props.get('port.direction') == 'in']
    desired = set()
    for input_id, input_props in inputs:
        channel = input_props.get('audio.channel')
        matching = [port_id for port_id, props in outputs if props.get('audio.channel') == channel]
        mono = [port_id for port_id, props in outputs if props.get('audio.channel') == 'MONO']
        if matching or mono:
            desired.add(((matching or mono)[0], input_id))
        elif channel == 'MONO':
            # A stereo network mic feeds a mono capture stream using one channel.
            left = [port_id for port_id, props in outputs if props.get('audio.channel') == 'FL']
            if left:
                desired.add((left[0], input_id))
    existing = set()
    for obj in objects:
        if obj.get('type') != 'PipeWire:Interface:Link':
            continue
        info = obj.get('info', {})
        if info.get('input-node-id') in target_ids:
            existing.add((info['output-port-id'], info['input-port-id']))
    # A missing/disconnected selected mic stays silent instead of falling back.
    for output, input_ in sorted(existing - desired):
        pipewire._run(['pw-link', '-d', str(output), str(input_)])
    for output, input_ in sorted(desired - existing):
        pipewire._run(['pw-link', str(output), str(input_)])


async def supervise() -> None:
    while True:
        try:
            await asyncio.to_thread(reconcile)
        except Exception:
            logger.exception('Could not restore selected microphone links')
        await asyncio.sleep(2)
