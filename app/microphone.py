"""Choose the microphone feeding desktop peers and restore its links after reconnects.

Desktop Roc and VBAN sinks remain unchanged. Only their input
links change. Android playback receivers are excluded so microphone audio is not
mixed into the phone's headset playback feed.

The choices:

- the headset's own mic,
- a USB mic plugged into the host (an AT2020 on the desk), by its ALSA card
  id so a replug or a profile change keeps the choice,
- "follow the headset's mute" with a USB mic: the headset mic while the
  headset's mic is live, the USB mic while it's muted or the headset is off.
  Muting the headset when sitting down at the desk switches to the desk mic,
- a phone in microphone mode.

Separately, all mics can be muted (`muted` in microphone.json): no mic feeds
the computers until it's unmuted. The headset's macro button can toggle it
(macro_button.py), since in follow mode the headset's own mute switches mics
instead of muting.

The headset's mute state comes from its HID channel (headset_hid.py).
Without a HyperX dongle Bragi can't tell the headset from a USB mic, so
"headset" is then any USB input, as before USB mics were choices.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

from . import headset_hid, headsets, peers, pipewire

logger = logging.getLogger('bragi.microphone')
_lock = threading.RLock()

# What the computers heard at the last reconcile, to tell the dashboards
# when it changes (a headset mute press in follow mode).
_last_live: str | None = None
# Set by main.py to ws.manager.broadcast_nowait. Called on the event loop only.
broadcast = None


def _settings() -> dict:
    path = peers.DATA_DIR / 'microphone.json'
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        data = {}
    return {'source': data.get('source') or 'headset', 'muted': data.get('muted') is True}


def _write(settings: dict) -> None:
    peers.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = peers.DATA_DIR / 'microphone.json'
    pending = path.with_suffix('.tmp')
    pending.write_text(json.dumps(settings))
    pending.replace(path)


def selected() -> str:
    return _settings()['source']


def muted() -> bool:
    return _settings()['muted']


def _nodes() -> dict[int, dict]:
    objects = json.loads(pipewire._run(['pw-dump']))
    return {obj['id']: obj.get('info', {}).get('props', {}) for obj in objects
            if obj.get('type') == 'PipeWire:Interface:Node'}


def _usb_inputs(nodes: dict[int, dict]) -> list[tuple[int, str, dict]]:
    """(node id, card id, props) of each USB capture device."""
    out = []
    for node_id, props in nodes.items():
        if props.get('media.class') != 'Audio/Source':
            continue
        card = headsets._card_id(props.get('node.name', ''), 'alsa_input.')
        if card:
            out.append((node_id, card, props))
    return out


def _is_headset(card: str) -> bool:
    return any(headset_hid.serial_matches(card, serial) for serial in headset_hid.serials())


def _headset_source(nodes: dict[int, dict]) -> int | None:
    inputs = _usb_inputs(nodes)
    known = [node_id for node_id, card, _ in inputs if _is_headset(card)]
    if known:
        return known[0]
    if headset_hid.serials():
        return None  # the HyperX dongle is there, its mic isn't
    return inputs[0][0] if inputs else None


def _usb_mics(nodes: dict[int, dict]) -> list[tuple[int, str, str]]:
    """(node id, card id, label) of USB mics that aren't the headset. Only
    known with a HyperX dongle to tell them apart."""
    if not headset_hid.serials():
        return []
    out = []
    for node_id, card, props in _usb_inputs(nodes):
        if _is_headset(card):
            continue
        label = props.get('node.description') or card
        for suffix in (' Analog Stereo', ' Mono', ' Multichannel Input'):
            label = label.removesuffix(suffix)
        out.append((node_id, card, label))
    return out


def headset_live() -> bool:
    """Whether a headset is on with its mic not muted. An unknown mute state
    counts as live, so a headset that doesn't report mute keeps working."""
    return any(view.get('headset_on') and view.get('mic_muted') is not True
               for view in (headset_hid._devices[s].view() for s in headset_hid.serials()))


def options(nodes: dict[int, dict] | None = None) -> list[dict]:
    nodes = _nodes() if nodes is None else nodes
    choices = [{'key': 'headset', 'label': 'Headset microphone'}]
    for _, card, label in _usb_mics(nodes):
        choices.append({'key': f'usb:{card}', 'label': label})
        choices.append({'key': f'follow:{card}',
                        'label': f'Headset when its mic is live, {label} when it\'s muted'})
    choices += [{'key': p.name, 'label': f'{p.name} microphone'} for p in peers.load_peers()
                if p.client_kind == 'android' and p.capture_mode == 'microphone']
    return choices


def _choose(nodes: dict[int, dict], source: str, registry) -> tuple[int | None, str]:
    """The node feeding the computers for this choice, and what to call it."""
    mics = {card: (node_id, label) for node_id, card, label in _usb_mics(nodes)}
    if source == 'headset':
        return _headset_source(nodes), 'Headset microphone'
    if source.startswith('usb:'):
        node_id, label = mics.get(source[4:], (None, 'USB mic (unplugged)'))
        return node_id, label
    if source.startswith('follow:'):
        node_id, label = mics.get(source[7:], (None, 'USB mic (unplugged)'))
        if headset_live():
            return _headset_source(nodes), 'Headset microphone (its mic is live)'
        return node_id, f'{label} (headset muted or off)'
    phone = next((p for p in registry if p.name == source and p.client_kind == 'android'
                  and p.capture_mode == 'microphone'), None)
    node_id = next((i for i, props in nodes.items() if phone is not None
                    and props.get('media.class') == 'Audio/Source'
                    and props.get('node.name') == phone.incoming_source_name), None)
    return node_id, f'{source} microphone'


def view(graph: pipewire.Graph | None = None) -> dict:
    """For the toolbar. Pass the page's graph to save a second pw-dump."""
    settings = _settings()
    if graph is not None:
        nodes = {n.id: {'node.name': n.name, 'media.class': n.media_class, 'node.description': n.description}
                 for n in graph.nodes}
    else:
        try:
            nodes = _nodes()
        except (pipewire.PipewireError, ValueError):
            nodes = {}
    choices = options(nodes)
    source = settings['source']
    if not any(choice['key'] == source for choice in choices):
        choices.append({'key': source, 'label': f'{source} microphone (unavailable)'})
    node_id, label = _choose(nodes, source, peers.load_peers())
    live = 'All mics muted' if settings['muted'] else label if node_id is not None else f'{label}: not connected'
    return {'source': source, 'options': choices, 'muted': settings['muted'], 'live': live}


def select(source: str) -> None:
    if not any(choice['key'] == source for choice in options()):
        raise ValueError('Choose the headset, a USB mic or a registered phone in microphone mode')
    with _lock:
        _write({**_settings(), 'source': source})
        reconcile()


def set_muted(on: bool) -> None:
    with _lock:
        _write({**_settings(), 'muted': bool(on)})
        reconcile()


def toggle_muted() -> bool:
    """Mutes or unmutes all mics. Returns whether they're muted now."""
    with _lock:
        on = not muted()
        set_muted(on)
        return on


def reconcile() -> bool:
    """Puts the links right. True when what the computers hear changed."""
    with _lock:
        return _reconcile()


def _reconcile() -> bool:
    global _last_live
    # Preserve the existing host links until the user first selects a microphone.
    if not (peers.DATA_DIR / 'microphone.json').exists():
        return False
    registry = peers.load_peers()
    objects = json.loads(pipewire._run(['pw-dump']))
    nodes = {obj['id']: obj.get('info', {}).get('props', {}) for obj in objects
             if obj.get('type') == 'PipeWire:Interface:Node'}
    settings = _settings()
    source_id, label = _choose(nodes, settings['source'], registry)
    if settings['muted']:
        source_id, label = None, 'All mics muted'
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
            # A stereo mic feeds a mono capture stream using one channel.
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
    live = label if source_id is not None else f'{label}: not connected'
    changed = live != _last_live
    if changed:
        logger.info('desktop mic: %s', live)
    _last_live = live
    return changed


def _tell() -> None:
    if broadcast is not None:
        broadcast({'type': 'microphone'})


async def on_headset_change() -> None:
    """A headset's state changed (its mute, or it turned off): in follow
    mode that can switch mics, so act now instead of at the next check."""
    try:
        if await asyncio.to_thread(reconcile):
            _tell()
    except Exception:
        logger.exception('Could not follow the headset mute')


async def supervise() -> None:
    while True:
        try:
            if await asyncio.to_thread(reconcile):
                _tell()
        except Exception:
            logger.exception('Could not restore selected microphone links')
        await asyncio.sleep(2)
