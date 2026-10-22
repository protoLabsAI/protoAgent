"""Fleet usage: cache-only fan-out, account identity, auth and paired transport."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from a2a_impl import auth
from graph.fleet import proxy, supervisor
from graph.providers.usage import Account, Window
from observability import usage_store
from observability.usage_store import UsageStore
from operator_api import telemetry_routes
from operator_api.usage_routes import register_usage_routes
from ops.usage import merge_fleet_usage, snapshot


def account(key="shared", used=20):
    return Account(
        "anthropic-oauth", "anthropic-oauth", account_key=key, plan="max", status="ok",
        windows=[Window("session", "session", 18000, used,
                        datetime.now(UTC) + timedelta(hours=1), "ok")],
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    store = UsageStore({}, path=tmp_path / "host.json")
    monkeypatch.setattr(usage_store, "_DEFAULT_STORE", store)
    monkeypatch.setattr(supervisor, "status", lambda: [])
    app = FastAPI()
    register_usage_routes(app)
    return TestClient(app)


def test_empty_roster_host_only(client):
    response = client.get("/api/usage/fleet")
    assert response.status_code == 200
    body = response.json()
    assert body["members"] == {"host": {"reachable": True}}
    assert len(body["accounts"]) == 2  # unknown identities never collapse together
    assert all(row["members"] == ["host"] for row in body["accounts"])


def test_shared_account_one_poll_and_members(client, monkeypatch, tmp_path):
    calls = []

    def fetch():
        calls.append("poll")
        return account()

    host_store = UsageStore({"anthropic-oauth": fetch}, path=tmp_path / "h.json")
    peer_store = UsageStore({"anthropic-oauth": fetch}, path=tmp_path / "p.json")
    # A passive observation identifies the peer's account without a provider poll.
    observed = account(used=80)
    peer_store.merge_windows(observed, observed.windows, "headers", datetime.now(UTC))
    monkeypatch.setattr(usage_store, "_DEFAULT_STORE", host_store)
    monkeypatch.setattr(supervisor, "status", lambda: [
        {"host": True, "id": "real-host-id"}, {"id": "worker"}, {"id": "worker"},
    ])
    reads = []

    async def read(slug, path):
        reads.append((slug, path))
        # Exercise the actual member route against a separate store, not a canned body.
        monkeypatch.setattr(usage_store, "_DEFAULT_STORE", peer_store)
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://member") as member:
            return (await member.get("/" + path)).json()

    monkeypatch.setattr(telemetry_routes, "_fetch_member_json", read)
    body = client.get("/api/usage/fleet").json()
    shared = next(row for row in body["accounts"] if row["account_key"] == "shared")
    assert shared["members"] == ["host", "worker"]
    assert shared["windows"][0]["used_pct"] == 20
    assert calls == ["poll"]
    assert reads == [("worker", "api/usage?cache_only=true")]


def test_cache_only_cold_store_never_polls_even_with_refresh(client, monkeypatch):
    def forbidden():
        pytest.fail("cached read must not poll")

    monkeypatch.setattr(usage_store, "_DEFAULT_STORE", UsageStore({"anthropic-oauth": forbidden}))
    result = client.get("/api/usage?cache_only=true&refresh=true")
    assert result.status_code == 200
    assert all(row["status"] == "unknown" for row in result.json()["accounts"])
    # Default snapshot behavior remains a regular on-demand read.
    assert snapshot(cache_only=True)["accounts"] == result.json()["accounts"]


@pytest.mark.parametrize("failure", [None, {"accounts": "bad"}, {"accounts": [None]}, RuntimeError("secret")])
def test_unreachable_is_contained(client, monkeypatch, failure):
    monkeypatch.setattr(supervisor, "status", lambda: [{"id": "bad"}, {"id": "good"}])

    async def read(slug, path):
        if slug == "good":
            return {"accounts": [account("other").to_dict()]}
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(telemetry_routes, "_fetch_member_json", read)
    response = client.get("/api/usage/fleet")
    assert response.status_code == 200
    assert response.json()["members"]["bad"] == {"reachable": False, "error": "usage unavailable"}
    assert response.json()["members"]["good"] == {"reachable": True}
    assert "secret" not in response.text


def test_remote_paired_bearer_and_redaction(client, monkeypatch):
    monkeypatch.setattr(supervisor, "status", lambda: [{"id": "remote", "remote": True}])
    monkeypatch.setattr(proxy, "_slug_cache", {})
    monkeypatch.setattr(proxy, "_remote_slugs", set())
    monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("remote", "https://remote.test", "paired-secret"))
    calls = []

    async def respond(request):
        calls.append(request)
        row = account("remote-account").to_dict()
        row["access_token"] = row["error"] = "paired-secret"
        row["windows"][0]["refresh_token"] = "paired-secret"
        row["windows"][0]["scope"] = {"model": "sonnet", "token": "paired-secret"}
        return httpx.Response(200, json={"accounts": [row]})

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(proxy, "_get_client", lambda: async_client)
    response = client.get("/api/usage/fleet")
    assert response.status_code == 200
    assert response.json()["members"]["remote"]["reachable"]
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://remote.test/api/usage?cache_only=true"
    assert request.headers["authorization"] == "Bearer paired-secret"
    assert request.extensions["timeout"]["read"] == 10.0
    assert "paired-secret" not in response.text


def test_merge_is_pure_host_preferred_and_unknowns_separate():
    host = {"accounts": [account().to_dict()]}
    peers = {"a": {"accounts": [account(used=90).to_dict(), account("peer").to_dict()]},
             "b": {"accounts": [account("peer").to_dict(), account(None).to_dict()]}}
    before = deepcopy((host, peers))
    body = merge_fleet_usage(host, peers)
    assert (host, peers) == before
    assert [row["members"] for row in body["accounts"]] == [["host", "a"], ["a", "b"], ["b"]]
    body["accounts"][0]["windows"][0]["used_pct"] = 99
    assert (host, peers) == before


def test_operator_only(client, monkeypatch):
    monkeypatch.setattr(auth, "_BEARER", ["operator-secret"])
    monkeypatch.setattr(auth, "_FEDERATION", ["federation-secret"])
    monkeypatch.setattr(auth, "_FLEET", [None])
    monkeypatch.setattr(auth, "_API_KEY", [""])
    client.app.add_middleware(auth.A2AAuthMiddleware)
    assert client.get("/api/usage/fleet").status_code == 401
    assert client.get("/api/usage/fleet", headers={"Authorization": "Bearer federation-secret"}).status_code == 403
    headers = {"Authorization": "Bearer operator-secret"}
    assert client.get("/api/usage/fleet", headers=headers).status_code == 200
    assert client.post("/api/usage/fleet", headers=headers).status_code == 405
