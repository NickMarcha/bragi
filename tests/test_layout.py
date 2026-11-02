"""Device order on the dashboard: shared by every browser, set by dragging."""
from fastapi.testclient import TestClient

from app import layout, views, ws
from app.main import app

from .fake_pipewire import HEADSET_CARD_ID, SECOND_CARD_ID


def order_of(state: dict) -> list[str]:
    return sorted(state["order_of"], key=state["order_of"].get)


def test_devices_default_to_headsets_then_peers(session):
    assert order_of(views.build_state()) == [
        f"headset:{HEADSET_CARD_ID}", f"headset:{SECOND_CARD_ID}",
        "peer:deck", "peer:studio", "peer:desktop"]


async def test_a_new_order_is_saved_and_sent_to_every_tab(session, broadcasts):
    order = ["peer:desktop", f"headset:{HEADSET_CARD_ID}", "peer:deck"]
    await ws.apply_action({"action": "set_order", "value": order})

    assert broadcasts.of_type("order") == [{"type": "order", "order": order}]
    # Devices the saved order doesn't mention keep their default order, after it.
    assert order_of(views.build_state()) == [
        "peer:desktop", f"headset:{HEADSET_CARD_ID}", "peer:deck",
        f"headset:{SECOND_CARD_ID}", "peer:studio"]


async def test_a_malformed_order_is_ignored(session, broadcasts):
    for value in (None, "peer:desktop", [1, 2], ["x" * 200], ["peer:a"] * 201):
        await ws.apply_action({"action": "set_order", "value": value})
    assert broadcasts.of_type("order") == []
    assert layout.get_order() == []


def test_cards_render_in_the_saved_order(session):
    layout.set_order(["peer:desktop", "peer:deck"])
    html = TestClient(app).get("/").text
    desktop = html[html.index('id="peer-desktop"'):]
    deck = html[html.index('id="peer-deck"'):]
    assert desktop[:desktop.index(">")].endswith('style="order: 0"')
    assert deck[:deck.index(">")].endswith('style="order: 1"')


def test_a_disabled_headset_is_parked_out_of_the_row(session):
    html = TestClient(app).get("/").text
    parked = html[html.index('id="parked"'):html.index("</main>")]
    assert f'data-headset="{SECOND_CARD_ID}"' in parked
    assert f'data-headset="{HEADSET_CARD_ID}"' not in parked


def test_a_peer_with_nothing_in_the_graph_is_parked_too(session):
    """A phone whose app isn't running has neither direction to mix."""
    from app import peers
    peers.register_android_peer("fairphone", "100.64.0.20", "microphone")
    html = TestClient(app).get("/").text
    parked = html[html.index('id="parked"'):html.index("</main>")]
    assert 'data-peer="fairphone"' in parked
    assert 'data-peer="desktop"' not in parked


def test_a_peer_parks_while_its_client_is_disconnected(session):
    """A phone's Roc modules live in peers.conf, so its nodes exist whether or
    not the phone is there; only the app's control socket says it is."""
    from app import peers
    from .fake_pipewire import FakeNode
    peers.register_android_peer("fairphone", "100.64.0.20", "none")
    session.free_nodes += [FakeNode(901, "fairphone-outgoing-sink", "fairphone", "Audio/Sink"),
                           FakeNode(902, "fairphone-incoming-source", "fairphone", "Stream/Output/Audio")]
    client = TestClient(app)

    def parked() -> str:
        html = client.get("/").text
        return html[html.index('id="parked"'):html.index("</main>")]

    assert 'data-peer="fairphone"' in parked()
    # Desktop peers too: deck's tray app holds the same kind of socket.
    assert 'data-peer="deck"' in parked()
    with client.websocket_connect("/ws/peer/fairphone?client=android") as phone, \
            client.websocket_connect("/ws/peer/deck"):
        phone.receive_json()
        assert 'data-peer="fairphone"' not in parked()
        assert 'data-peer="deck"' not in parked()
