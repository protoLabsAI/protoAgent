"""Per-view publish capabilities leave operator publishers compatible."""
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.routes import register_operator_routes


@pytest.fixture
def publisher(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("operator_api.routes.monotonic", lambda: now[0])
    app = FastAPI()
    publish = Mock()
    register_operator_routes(
        app, runtime_status=lambda: {}, subagent_list=lambda: [],
        subagent_run=Mock(), subagent_batch=Mock(), events_publish=publish,
    )
    with TestClient(app) as client:
        yield client, publish, now


def mint(client, plugin="cadview"):
    response = client.post("/api/events/publish-token", json={"plugin_id": plugin})
    assert response.status_code == 200
    return {"X-Plugin-View-Token": response.json()["token"]}


def test_namespace_and_plain_publish(publisher):
    client, publish, _ = publisher
    headers = mint(client)
    for topic, status in [("cadview.status", 200), ("other.status", 403), ("cadview2.status", 403)]:
        assert client.post("/api/events/publish", headers=headers, json={"topic": topic}).status_code == status
    assert client.post("/api/events/publish", json={"topic": "other.status"}).status_code == 200
    assert publish.call_count == 2


def test_bucket_refill_and_independent_views(publisher):
    client, publish, now = publisher
    headers = mint(client)
    body = {"topic": "cadview.status"}
    for _ in range(20):
        assert client.post("/api/events/publish", headers=headers, json=body).status_code == 200
    assert client.post("/api/events/publish", headers=headers, json=body).status_code == 429
    assert client.post("/api/events/publish", headers=mint(client), json=body).status_code == 200
    assert client.post("/api/events/publish", json=body).status_code == 200
    now[0] += 0.11
    assert client.post("/api/events/publish", headers=headers, json=body).status_code == 200
    assert publish.call_count == 23


def test_invalid_expired_tokens_and_existing_validation(publisher):
    client, publish, now = publisher
    headers = mint(client)
    for token in ("", "made-up"):
        assert client.post("/api/events/publish", headers={"X-Plugin-View-Token": token},
                           json={"topic": "cadview.status"}).status_code == 403
    for body, status in [({"topic": "cadview"}, 400), ({"topic": "cadview.*"}, 400),
                         ({"topic": "cadview.x", "data": [1]}, 400),
                         ({"topic": "cadview.x", "data": {"x": "x" * 65536}}, 413)]:
        assert client.post("/api/events/publish", headers=headers, json=body).status_code == status
    now[0] += 86400
    assert client.post("/api/events/publish", headers=headers,
                       json={"topic": "cadview.status"}).status_code == 403
    assert client.post("/api/events/publish-token", json={"plugin_id": "bad.id"}).status_code == 400
    publish.assert_not_called()
