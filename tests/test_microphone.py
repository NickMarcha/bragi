"""Microphone selection rewires only desktop microphone destinations."""
import json

import pytest
from fastapi.testclient import TestClient
from app import headset_hid, microphone, peers, pipewire
from app.main import app


class RoutingHost:
    def __init__(self):
        self.calls = []
        self.links = {(301, 101), (302, 102), (301, 201), (302, 202), (301, 501), (302, 502), (301, 601)}
        self.nodes = [
            (1, 'deck-test-sink', 'Audio/Sink'),
            (2, 'studio-test-sink', 'Audio/Sink'),
            (3, 'old-loopback', 'Stream/Output/Audio'),
            (4, 'alsa_input.usb-headset.mono', 'Audio/Source'),
            (5, 'desktop-outgoing-sink', 'Audio/Sink'),
            (6, 'fairphone-outgoing-sink', 'Audio/Sink'),
            (7, 'fairphone-incoming-source', 'Audio/Source'),
        ]
        self.ports = [(101, 1, 'in', 'FL'), (102, 1, 'in', 'FR'),
                      (201, 2, 'in', 'FL'), (202, 2, 'in', 'FR'),
                      (301, 3, 'out', 'FL'), (302, 3, 'out', 'FR'),
                      (401, 4, 'out', 'MONO'), (501, 5, 'in', 'FL'), (502, 5, 'in', 'FR'),
                      (601, 6, 'in', 'FL'), (701, 7, 'out', 'FL'), (702, 7, 'out', 'FR')]

    def run(self, args, input_text=None):
        self.calls.append(args)
        if args[0] == 'pw-link':
            pair = tuple(map(int, args[-2:]))
            if '-d' in args:
                self.links.remove(pair)
            else:
                self.links.add(pair)
            return ''
        assert args == ['pw-dump']
        objects = [{'id': i, 'type': 'PipeWire:Interface:Node', 'info': {'props': {'node.name': name, 'media.class': kind}}}
                   for i, name, kind in self.nodes]
        objects += [{'id': i, 'type': 'PipeWire:Interface:Port', 'info': {'props': {'node.id': node, 'port.direction': direction, 'audio.channel': channel}}}
                    for i, node, direction, channel in self.ports]
        owners = {i: node for i, node, _, _ in self.ports}
        objects += [{'id': 1000 + index, 'type': 'PipeWire:Interface:Link', 'info': {
            'output-node-id': owners[out], 'input-node-id': owners[in_], 'output-port-id': out, 'input-port-id': in_}}
                    for index, (out, in_) in enumerate(sorted(self.links))]
        return json.dumps(objects)


def test_phone_microphone_feeds_desktops_and_vban_peers_but_not_phone_playback(session, monkeypatch):
    peers.register_android_peer('fairphone', '100.64.0.20', 'microphone')
    host = RoutingHost()
    monkeypatch.setattr(pipewire, '_run', host.run)
    client = TestClient(app)
    assert client.post('/audio/microphone', data={'source': 'fairphone'}).status_code == 200
    assert host.links == {(701, 101), (702, 102), (701, 201), (702, 202), (701, 501), (702, 502), (301, 601)}
    assert microphone.selected() == 'fairphone'
    host.calls.clear()
    microphone.reconcile()
    assert host.calls == [['pw-dump']]
    # Recreated original links after a reconnect are removed again.
    host.links.add((301, 101))
    microphone.reconcile()
    assert (301, 101) not in host.links
    assert client.post('/audio/microphone', data={'source': 'headset'}).status_code == 200
    assert host.links == {(401, 101), (401, 102), (401, 201), (401, 202), (401, 501), (401, 502), (301, 601)}


def test_missing_selected_phone_mic_stays_silent_and_device_audio_is_not_a_mic(session, monkeypatch):
    peers.register_android_peer('fairphone', '100.64.0.20', 'microphone')
    host = RoutingHost()
    monkeypatch.setattr(pipewire, '_run', host.run)
    microphone.select('fairphone')
    host.nodes = [node for node in host.nodes if node[0] != 7]
    host.ports = [port for port in host.ports if port[1] != 7]
    host.links = {(401, 501), (401, 502), (301, 601)}
    microphone.reconcile()
    assert host.links == {(301, 601)}
    peer = peers.load_peers()[-1]
    peer.capture_mode = 'device_audio'
    peers.save_peers([*peers.load_peers()[:-1], peer])
    client = TestClient(app)
    assert client.post('/audio/microphone', data={'source': 'fairphone'}).status_code == 422
    assert 'unavailable' in client.get('/audio/microphone').text


def test_original_host_routing_is_untouched_until_selection_is_used(session):
    microphone.reconcile()
    assert session.calls == []


# --- a USB desk mic, following the headset's mute ---------------------------------

HEADSET_IN = 'alsa_input.usb-HP__Inc_HyperX_Cloud_III_S_Wireless_SER1-00.mono-fallback'
AT2020_IN = 'alsa_input.usb-audio-technica_AT2020USB_-00.analog-stereo'
AT2020 = 'usb-audio-technica_AT2020USB_-00'


class DeskHost(RoutingHost):
    """The headset's mic (node 4) and an AT2020 (node 8) on the host."""

    def __init__(self):
        super().__init__()
        self.nodes = [n if n[0] != 4 else (4, HEADSET_IN, 'Audio/Source') for n in self.nodes]
        self.nodes.append((8, AT2020_IN, 'Audio/Source'))
        self.ports += [(801, 8, 'out', 'FL'), (802, 8, 'out', 'FR')]

    def run(self, args, input_text=None):
        out = super().run(args, input_text)
        if args == ['pw-dump']:
            objects = json.loads(out)
            for obj in objects:
                if obj['id'] == 8:
                    obj['info']['props']['node.description'] = 'AT2020USB+ Analog Stereo'
            out = json.dumps(objects)
        return out


@pytest.fixture
def desk(session, monkeypatch):
    host = DeskHost()
    monkeypatch.setattr(pipewire, '_run', host.run)
    hs = headset_hid.HidHeadset('SER1', lambda data: None, lambda serial: None)
    hs._state.update(headset_on=True, mic_muted=False)
    monkeypatch.setattr(headset_hid, '_devices', {'SER1': hs})
    monkeypatch.setattr(microphone, '_last_live', None)
    host.headset = hs
    return host


FROM_HEADSET = {(401, 101), (401, 102), (401, 201), (401, 202), (401, 501), (401, 502), (301, 601)}
FROM_AT2020 = {(801, 101), (802, 102), (801, 201), (802, 202), (801, 501), (802, 502), (301, 601)}


def test_a_usb_mic_is_a_choice_and_the_headset_is_still_its_own_mic(desk):
    keys = {o['key']: o['label'] for o in microphone.options()}
    assert keys[f'usb:{AT2020}'] == 'AT2020USB+'
    assert keys[f'follow:{AT2020}'] == "Headset when its mic is live, AT2020USB+ when it's muted"
    microphone.select('headset')
    assert desk.links == FROM_HEADSET   # not the AT2020, though it's a USB input too
    microphone.select(f'usb:{AT2020}')
    assert desk.links == FROM_AT2020


def test_following_the_headset_mute_switches_between_the_mics(desk):
    microphone.select(f'follow:{AT2020}')
    assert desk.links == FROM_HEADSET
    assert microphone.view()['live'] == 'Headset microphone (its mic is live)'

    desk.headset._state['mic_muted'] = True      # sat down at the desk
    assert microphone.reconcile() is True        # changed: the dashboards are told
    assert desk.links == FROM_AT2020
    assert microphone.view()['live'] == 'AT2020USB+ (headset muted or off)'
    assert microphone.reconcile() is False

    desk.headset._state.update(headset_on=False, mic_muted=None)   # headset off
    microphone.reconcile()
    assert desk.links == FROM_AT2020

    desk.headset._state.update(headset_on=True, mic_muted=False)   # walked away again
    microphone.reconcile()
    assert desk.links == FROM_HEADSET


def test_muting_all_mics_silences_every_choice_until_unmuted(desk):
    microphone.select(f'follow:{AT2020}')
    assert microphone.toggle_muted() is True
    assert desk.links == {(301, 601)}
    desk.headset._state['mic_muted'] = True
    microphone.reconcile()
    assert desk.links == {(301, 601)}             # still muted on the desk mic
    assert microphone.view()['live'] == 'All mics muted'
    assert microphone.toggle_muted() is False
    assert desk.links == FROM_AT2020


def test_an_unplugged_desk_mic_stays_silent_rather_than_using_the_muted_headset(desk):
    microphone.select(f'follow:{AT2020}')
    desk.headset._state['mic_muted'] = True
    desk.nodes = [n for n in desk.nodes if n[0] != 8]
    desk.ports = [p for p in desk.ports if p[1] != 8]
    desk.links = {link for link in desk.links if link[0] not in (801, 802)}
    microphone.reconcile()
    assert desk.links == {(301, 601)}
    assert microphone.view()['live'].endswith('not connected')


def test_the_mute_route_and_the_macro_action(desk):
    from app import macro_button
    import asyncio
    microphone.select('headset')
    client = TestClient(app)
    page = client.post('/audio/microphone/mute', data={'muted': '1'})
    assert page.status_code == 200 and 'Unmute mics' in page.text
    assert desk.links == {(301, 601)}
    assert macro_button.valid_action({'type': 'toggle_mic_mute'}) == {'type': 'toggle_mic_mute'}
    result = asyncio.run(macro_button.press('SER1', 'single', {'type': 'toggle_mic_mute'}))
    assert result['ok'] and result['text'] == 'mics live'
    assert desk.links == FROM_HEADSET
