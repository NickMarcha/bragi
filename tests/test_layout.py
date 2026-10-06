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
        "peer:sagedeck", "peer:sage-dev", "peer:sage"]


async def test_a_new_order_is_saved_and_sent_to_every_tab(session, broadcasts):
    order = ["peer:sage", f"headset:{HEADSET_CARD_ID}", "peer:sagedeck"]
    await ws.apply_action({"action": "set_order", "value": order})

    assert broadcasts.of_type("order") == [{"type": "order", "order": order}]
    # Devices the saved order doesn't mention keep their default order, after it.
    assert order_of(views.build_state()) == [
        "peer:sage", f"headset:{HEADSET_CARD_ID}", "peer:sagedeck",
        f"headset:{SECOND_CARD_ID}", "peer:sage-dev"]


async def test_a_malformed_order_is_ignored(session, broadcasts):
    for value in (None, "peer:sage", [1, 2], ["x" * 200], ["peer:a"] * 201):
        await ws.apply_action({"action": "set_order", "value": value})
    assert broadcasts.of_type("order") == []
    assert layout.get_order() == []


def test_cards_render_in_the_saved_order(session):
    layout.set_order(["peer:sage", "peer:sagedeck"])
    html = TestClient(app).get("/").text
    sage = html[html.index('id="peer-sage"'):]
    sagedeck = html[html.index('id="peer-sagedeck"'):]
    assert sage[:sage.index(">")].endswith('style="order: 0"')
    assert sagedeck[:sagedeck.index(">")].endswith('style="order: 1"')


def test_a_disabled_headset_is_parked_out_of_the_row(session):
    html = TestClient(app).get("/").text
    parked = html[html.index('id="parked"'):html.index("</main>")]
    assert f'data-headset="{SECOND_CARD_ID}"' in parked
    assert f'data-headset="{HEADSET_CARD_ID}"' not in parked


def test_a_peer_with_nothing_in_the_graph_is_parked_too(session):
    """A phone whose app isn't running has neither direction to mix."""
    from app import peers
    peers.register_android_peer("fairphone", "100.98.253.67", "microphone")
    html = TestClient(app).get("/").text
    parked = html[html.index('id="parked"'):html.index("</main>")]
    assert 'data-peer="fairphone"' in parked
    assert 'data-peer="sagedeck"' not in parked
