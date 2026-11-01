"""The /api/v1 API other apps use: keys, listing, changing controls, the
explainer, and the dashboard socket with a key."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import audio_state, auth, pipewire, views
from app.main import app

from .fake_pipewire import HEADSET_CARD_ID, SECOND_CARD_ID


@pytest.fixture
def api(session):
    _, key = auth.create_api_key("test app")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {key}"
    client.key = key
    return client


def next_of_type(socket, msg_type: str) -> dict:
    message = socket.receive_json()
    while message["type"] != msg_type:
        message = socket.receive_json()
    return message


def test_explainer_is_public_and_uses_the_callers_address(session):
    response = TestClient(app).get("/api/v1/help", headers={"host": "sagepi.example", "x-forwarded-proto": "https"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "https://sagepi.example/api/v1/devices" in response.text
    assert "wss://sagepi.example/ws?token=" in response.text


def test_api_needs_a_key_even_without_a_password(session):
    client = TestClient(app)
    assert client.get("/api/v1/devices").status_code == 401
    assert client.get("/api/v1/devices", headers={"Authorization": "Bearer bragi_wrong"}).status_code == 401


def test_a_session_cookie_is_not_an_api_key(session):
    auth.set_password("hunter22")
    client = TestClient(app)
    client.post("/auth/login", data={"password": "hunter22"})
    assert client.get("/api/v1/devices").status_code == 401


def test_key_works_in_every_documented_place(api):
    plain = TestClient(app)
    assert plain.get("/api/v1/devices", headers={"X-API-Key": api.key}).status_code == 200
    assert plain.get(f"/api/v1/devices?token={api.key}").status_code == 200


def test_list_devices(api):
    devices = api.get("/api/v1/devices").json()["devices"]
    ids = [d["id"] for d in devices]
    assert ids == [f"headset:{HEADSET_CARD_ID}", f"headset:{SECOND_CARD_ID}",
                   "peer:sagedeck", "peer:sage-dev", "peer:sage"]
    headset = devices[0]
    assert headset["enabled"] is True
    assert set(headset["controls"]) == {"playback", "capture"}
    assert headset["controls"]["playback"]["pannable"] is False
    assert "balance" not in headset["controls"]["playback"]
    sage = devices[-1]
    assert sage["controls"]["incoming"]["pannable"] is True
    assert sage["controls"]["incoming"]["balance"] == 0.0


def test_get_one_device_and_a_missing_one(api):
    assert api.get("/api/v1/devices/peer/sage").json()["id"] == "peer:sage"
    assert api.get("/api/v1/devices/peer/nobody").status_code == 404
    assert api.get("/api/v1/devices/speaker/x").status_code == 404


def test_setting_a_peer_volume_returns_the_result_and_tells_dashboards(api):
    with api.websocket_connect(f"/ws?token={api.key}") as socket:
        assert socket.receive_json()["type"] == "state"
        response = api.patch("/api/v1/devices/peer/sage/controls/incoming",
                             json={"volume": 0.5, "balance": -0.25})
        assert response.status_code == 200
        body = response.json()
        assert body["requested"] == {"volume": 0.5, "balance": -0.25}
        assert body["control"]["volume"] == 0.5
        assert body["control"]["balance"] == -0.25
        assert body["control"]["connected"] is True

        message = next_of_type(socket, "control")
        assert (message["key"], message["direction"], message["volume"]) == ("sage", "incoming", 0.5)
    assert audio_state.get_state("sage-incoming-source") == (0.5, -0.25)


def test_setting_a_headset_volume_and_mute(api):
    response = api.patch(f"/api/v1/devices/headset/{HEADSET_CARD_ID}/controls/playback",
                         json={"volume": 0.3, "muted": True})
    assert response.status_code == 200
    control = response.json()["control"]
    assert control["volume"] == pytest.approx(0.3)
    assert control["muted"] is True
    pipewire.forget_volumes()
    assert pipewire.get_volume_mute(233) == (pytest.approx(0.3), True)


def test_bad_changes_are_refused(api):
    url = f"/api/v1/devices/headset/{HEADSET_CARD_ID}/controls/playback"
    assert api.patch(url, json={"volume": 2}).status_code == 422
    assert api.patch(url, json={}).status_code == 422
    assert api.patch(url, json={"balance": 0.5}).status_code == 422
    assert api.patch(f"/api/v1/devices/headset/{HEADSET_CARD_ID}/controls/incoming",
                     json={"volume": 0.5}).status_code == 404
    # The second headset is switched off, so its controls have no node.
    assert api.patch(f"/api/v1/devices/headset/{SECOND_CARD_ID}/controls/playback",
                     json={"volume": 0.5}).status_code == 409


def test_switching_a_headset_on(api):
    response = api.patch(f"/api/v1/devices/headset/{SECOND_CARD_ID}", json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["device"]["enabled"] is True
    hs, device = views.get_headset_with_device(pipewire.dump(), SECOND_CARD_ID)
    assert views.headset_view(hs, device)["enabled"] is True


def test_revoked_key_stops_working(api):
    key_id = auth.list_api_keys()[0]["id"]
    assert auth.revoke_api_key(key_id)
    assert api.get("/api/v1/devices").status_code == 401


def test_dashboard_socket_refuses_a_wrong_key_even_without_a_password(session):
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect("/ws?token=bragi_wrong"):
            pass


def test_set_mute_action_on_the_socket(api):
    with api.websocket_connect(f"/ws?token={api.key}") as socket:
        socket.receive_json()
        socket.send_json({"action": "set_mute", "target": "peer", "key": "sage",
                          "direction": "incoming", "value": True})
        assert next_of_type(socket, "control")["muted"] is True


def test_keys_are_managed_from_the_dashboard_and_shown_once(session):
    client = TestClient(app)
    created = client.post("/auth/api-keys", json={"name": "stream deck"}).json()
    assert created["key"].startswith("bragi_")
    listed = client.get("/auth/settings").json()["api_keys"]
    assert [k["name"] for k in listed] == ["stream deck"]
    assert "key" not in listed[0] and "hash" not in listed[0]
    assert client.delete(f"/auth/api-keys/{created['id']}").status_code == 200
    assert client.get("/auth/settings").json()["api_keys"] == []
