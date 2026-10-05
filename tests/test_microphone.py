"""Microphone selection rewires only desktop microphone destinations."""
import json

from fastapi.testclient import TestClient
from app import microphone, peers, pipewire
from app.main import app


class RoutingHost:
    def __init__(self):
        self.calls = []
        self.links = {(301, 101), (302, 102), (301, 201), (302, 202), (301, 501), (302, 502), (301, 601)}
        self.nodes = [
            (1, 'sagedeck-test-sink', 'Audio/Sink'),
            (2, 'sagedev-test-sink', 'Audio/Sink'),
            (3, 'old-loopback', 'Stream/Output/Audio'),
            (4, 'alsa_input.usb-headset.mono', 'Audio/Source'),
            (5, 'sage-outgoing-sink', 'Audio/Sink'),
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
    peers.register_android_peer('fairphone', '100.98.253.67', 'microphone')
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
    peers.register_android_peer('fairphone', '100.98.253.67', 'microphone')
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
