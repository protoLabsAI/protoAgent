"""Tool usage REST and CLI contracts, without live servers or fleet members."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from a2a_impl import auth
from deck import hub
from graph import usage_cli
from observability.telemetry_store import TelemetryStore
from operator_api.usage_routes import register_usage_routes
from runtime.state import STATE


def tool(name="search", calls=4, errors=1, p50=10, p95=30):
    return {"tool": name, "calls": calls, "errors": errors, "error_rate": errors / calls,
            "p50_ms": p50, "p95_ms": p95}


@pytest.fixture
def api(tmp_path, monkeypatch):
    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    monkeypatch.setattr(STATE, "telemetry_store", store)
    app = FastAPI()
    register_usage_routes(app)
    return TestClient(app), store


def test_route_summary_and_since(api):
    client, store = api
    now = datetime.now(UTC)
    for name, days in [("old", 10), ("new", 1)]:
        store.record_tool_call({"tool": name, "ts": (now - timedelta(days=days)).isoformat(), "duration_ms": 12, "ok": 1})
    assert client.get("/api/telemetry/tools").json() == {
        "enabled": True, "since": None, "tools": store.tool_summary(),
    }
    for since in ["7d", (now - timedelta(days=7)).isoformat()]:
        response = client.get("/api/telemetry/tools", params={"since": since})
        assert response.status_code == 200
        assert response.json()["tools"] == [tool("new", 1, 0, 12, 12)]
    assert client.get("/api/telemetry/tools", params={"since": "bad"}).status_code == 422
    assert client.get("/api/telemetry/tools", params={"since": "999999999999999999d"}).status_code == 422


def test_route_disabled(api, monkeypatch):
    monkeypatch.setattr(STATE, "telemetry_store", None)
    assert api[0].get("/api/telemetry/tools").json() == {"enabled": False, "since": None, "tools": []}


def test_route_operator_only(api, monkeypatch):
    client, _ = api
    for key, value in [("_BEARER", ["operator-secret"]), ("_FEDERATION", ["federation-secret"]),
                       ("_FLEET", [None]), ("_API_KEY", [""])]:
        monkeypatch.setattr(auth, key, value)
    client.app.add_middleware(auth.A2AAuthMiddleware)
    path = "/api/telemetry/tools"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer federation-secret"}).status_code == 403
    assert client.get(path, headers={"Authorization": "Bearer operator-secret"}).status_code == 200


class Client:
    url = "http://127.0.0.1:7870"

    def __init__(self, bodies):
        self.bodies = bodies
        self.calls = []
        self.closed = False

    def member_get(self, slug, path, **params):
        self.calls.append((slug, path, params))
        value = self.bodies[slug]
        if isinstance(value, Exception):
            raise value
        return {"enabled": True, "tools": value}

    def close(self):
        self.closed = True


def connect(monkeypatch, bodies):
    client = Client(bodies)
    roster = [{"name": slug, "id": slug, "host": slug == "host", "running": slug != "offline"} for slug in bodies]
    monkeypatch.setattr(hub, "connect", lambda **kwargs: SimpleNamespace(client=client, roster=roster))
    monkeypatch.setattr(usage_cli, "_now", lambda: datetime(2026, 11, 6, tzinfo=UTC))
    return client


TOP_KEYS = {"mode", "ok", "hub", "since", "percentiles", "tools", "unreachable", "error"}


def test_local_schema_since_and_sorted_table(monkeypatch, capsys):
    client = connect(monkeypatch, {"host": [tool("small", 2), tool("large", 10), tool("alpha", 2)]})
    assert usage_cli.run_usage_cli(["--by-tool", "--since", "7d", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert set(body) == TOP_KEYS
    assert body["percentiles"] == "local" and body["ok"]
    assert body["since"] == "2026-10-30T00:00:00+00:00"
    assert body["tools"] == [tool("large", 10), tool("alpha", 2), tool("small", 2)]
    assert client.calls == [("host", "/api/telemetry/tools", {"since": body["since"]})]
    assert client.closed
    assert usage_cli.run_usage_cli(["--by-tool"]) == 0
    out = capsys.readouterr().out
    assert out.index("large") < out.index("alpha") < out.index("small")


def test_fleet_aggregate_and_old_member(monkeypatch, capsys):
    client = connect(monkeypatch, {
        "host": [tool()], "worker": [tool(calls=6, errors=0, p50=20, p95=25)],
        "old": hub.HubRequestError(Client.url, 404, "missing"),
        "broken": hub.HubRequestError(Client.url, 503, "unavailable"), "offline": [],
    })
    assert usage_cli.run_usage_cli(["--by-tool", "--fleet", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert set(body) == TOP_KEYS
    assert body["tools"] == [tool(calls=10, errors=1, p50=20, p95=30)]
    assert body["percentiles"] == "max_of_members"
    reasons = {row["slug"]: row["error"] for row in body["unreachable"]}
    assert reasons == {"old": "no tool telemetry", "broken": "unavailable", "offline": "stopped"}
    assert "offline" not in [call[0] for call in client.calls]
    assert usage_cli.run_usage_cli(["--by-tool", "--fleet"]) == 0
    output = capsys.readouterr()
    assert "max of members" in output.out
    assert "old: no tool telemetry" in output.err


def test_tool_connection_failure_schema(monkeypatch, capsys):
    def fail(**kwargs):
        raise hub.NoHub([], [])
    monkeypatch.setattr(hub, "connect", fail)
    assert usage_cli.run_usage_cli(["--by-tool", "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert set(body) == TOP_KEYS
    assert not body["ok"] and body["error"] and body["tools"] == []


@pytest.mark.parametrize("args", [["--since", "7d"], ["--by-tool", "--by-agent"],
                                 ["--by-tool", "--account", "a"], ["--by-tool", "--since", "bad"]])
def test_invalid_options(args):
    with pytest.raises(SystemExit) as exc:
        usage_cli.run_usage_cli(args)
    assert exc.value.code == 2
