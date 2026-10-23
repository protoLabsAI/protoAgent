"""Host-free attribution: migrations, native/ACP recording, window shares and auth."""
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph import delegation_usage, usage_cli
from graph.config import LangGraphConfig
from graph.providers import oauth
from graph.providers.usage import accounts
from observability.telemetry_store import TelemetryStore
from operator_api.usage_routes import register_usage_routes
from ops import usage
from runtime.state import STATE
from server.turn_telemetry import record_turn


@pytest.fixture
def store(tmp_path, monkeypatch):
    value = TelemetryStore(str(tmp_path / "turns.db"))
    monkeypatch.setattr(STATE, "telemetry_store", value)
    monkeypatch.setattr(STATE, "graph_config", None)
    # Every credential seam is fake, including the forbidden refresh paths.
    for provider in ("anthropic", "codex"):
        monkeypatch.setattr(oauth, f"peek_{provider}_access_token",
                            lambda: SimpleNamespace(account_id="shared"))
        monkeypatch.setattr(oauth, f"resolve_{provider}_oauth",
                            lambda: pytest.fail("must not refresh"))
    return value


@pytest.mark.parametrize("old_primary_key", [False, True])
def test_migration_preserves_rows_and_is_idempotent(tmp_path, old_primary_key):
    path = str(tmp_path / "old.db")
    TelemetryStore(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE turns DROP COLUMN provider_id")
        db.execute("ALTER TABLE turns DROP COLUMN account_key")
        if old_primary_key:
            # Reproduce the pre-row_id schema as well as the recent schema.
            db.execute("ALTER TABLE turns RENAME TO original")
            columns = db.execute("PRAGMA table_info(original)").fetchall()
            declarations = [f'{r[1]} {r[2]}' + (' PRIMARY KEY' if r[1] == 'task_id' else '')
                            for r in columns if r[1] != 'row_id']
            db.execute("CREATE TABLE turns (" + ",".join(declarations) + ")")
            db.execute("DROP TABLE original")
        db.execute("INSERT INTO turns (task_id, total_tokens) VALUES ('old', 123)")
    first = TelemetryStore(path).recent()
    assert first[0]["total_tokens"] == 123
    assert first[0]["provider_id"] is None and first[0]["account_key"] is None
    assert TelemetryStore(path).recent() == first


@pytest.mark.parametrize("provider", ["anthropic-oauth", "openai-codex", "openai"])
def test_turn_identity(store, monkeypatch, provider):
    config = LangGraphConfig.from_dict({"model": {"provider": provider, "name": "model"}})
    monkeypatch.setattr(STATE, "graph_config", config)
    record_turn(task_id="turn", session_id="s", state="completed", models=["model"],
                usage={"input_tokens": 20, "output_tokens": 10}, publish_usage_event=False)
    row = store.recent()[0]
    assert row["total_tokens"] == 30
    assert (row["provider_id"], row["account_key"]) == (("", "") if provider == "openai" else (provider, "shared"))


def test_explicit_override_and_raising_resolver(store, monkeypatch):
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig.app_defaults())
    record_turn(task_id="override", session_id="", state="completed", models=["gpt"],
                requested_model="openai-codex:gpt", publish_usage_event=False)
    assert store.recent()[0]["provider_id"] == "openai-codex"

    def broken():
        raise RuntimeError("credential unavailable")

    monkeypatch.setattr(oauth, "peek_codex_access_token", broken)
    record_turn(task_id="broken", session_id="", state="completed", models=["openai-codex:gpt"],
                usage={"input_tokens": 17}, publish_usage_event=False)
    row = store.recent()[0]
    assert row["task_id"] == "broken" and row["total_tokens"] == 17
    assert row["account_key"] == row["provider_id"] == ""


@pytest.mark.parametrize("resolved", [True, False])
def test_acp_durable_row(store, monkeypatch, resolved):
    from plugins.coding_agent.acp_client import AcpClient

    monkeypatch.setattr(accounts, "_configured_delegates", lambda: [
        {"name": "coder", "command": "codex-acp" if resolved else "unknown"},
    ])
    client = AcpClient("unused", [], name="coder", cwd=".")
    client._record_run_telemetry("completed", time.monotonic(), 3, "session", {
        "inputTokens": 10, "outputTokens": 5, "cachedReadTokens": 20, "cachedWriteTokens": 2,
    })
    rows = store.recent()
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == 0
    assert rows[0]["total_tokens"] == (37 if resolved else 0)
    assert rows[0]["account_key"] == ("shared" if resolved else "")
    assert rows[0]["tool_calls"] == 3


def snapshot(key="shared"):
    return {"accounts": [{"account_key": key, "provider_id": "openai-codex", "status": "ok",
                           "windows": [{"id": "session", "kind": "session", "used_pct": 40,
                                        "window_seconds": 18000,
                                        "resets_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}]}]}


def test_window_counts_include_cache_exclude_old_unknown_and_other_models(store):
    body = snapshot()
    windows = usage.attribution_windows(body["accounts"])
    for key, tokens, when, model in [
        ("shared", 30, datetime.now(UTC), "gpt"),
        ("shared", 99, datetime.now(UTC) - timedelta(days=1), "gpt"),
        (None, 100, datetime.now(UTC), "gpt"),
        ("other", 100, datetime.now(UTC), "gpt"),
        ("shared", 10, datetime.now(UTC), "other"),
    ]:
        store.record({"task_id": "t", "account_key": key, "total_tokens": tokens,
                      "ended_at": when.isoformat(), "model": model})
    assert usage.local_attribution(windows)[0]["tokens"] == 40
    windows[0]["model"] = "gpt"
    assert usage.local_attribution(windows)[0]["tokens"] == 30


def test_merge_shares_redacts_and_marks_unavailable():
    body = snapshot()
    windows = usage.attribution_windows(body["accounts"])
    members = {"a": {"attribution": [{**windows[0], "tokens": 10, "secret": "secret"}]},
               "b": {"attribution": [{**windows[0], "tokens": 30}]}, "old": {"accounts": []}}
    usage.merge_attribution(body["accounts"], windows, members)
    result = body["accounts"][0]["by_agent"][0]
    assert [m["share_pct"] for m in result["members"]] == [25, 75]
    assert result["unavailable"] == ["old"]
    assert "secret" not in json.dumps(body)
    for payload in members.values():
        for row in payload.get("attribution", []):
            row["tokens"] = 0
    usage.merge_attribution(body["accounts"], windows, members)
    assert all(m["share_pct"] is None for m in body["accounts"][0]["by_agent"][0]["members"])


@pytest.mark.parametrize("fleet", [False, True])
def test_cli_uses_operator_route_and_labels_shares(monkeypatch, capsys, fleet):
    body = snapshot()
    windows = usage.attribution_windows(body["accounts"])
    usage.merge_attribution(body["accounts"], windows,
                            {"worker": {"attribution": [{**windows[0], "tokens": 40}]}})
    calls = []

    def get(slug, path, **params):
        calls.append((slug, path, params))
        return body

    client = SimpleNamespace(url="http://fake", member_get=get, close=lambda: None)
    monkeypatch.setattr(usage_cli.deckhub, "connect", lambda **kw: SimpleNamespace(client=client))
    assert usage_cli.run_usage_cli(["--by-agent"] + (["--fleet"] if fleet else [])) == 0
    output = capsys.readouterr().out
    assert "share of attributed tokens" in output and "100.0%" in output
    assert calls == [("host", "/api/usage/fleet" if fleet else "/api/usage", {"by_agent": "true"})]


def test_fleet_includes_peer_only_accounts_and_operator_auth(store, monkeypatch):
    from a2a_impl import auth
    from graph.fleet import supervisor
    from operator_api import telemetry_routes
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setattr(usage, "snapshot", lambda *a, **kw: {"accounts": []})
    monkeypatch.setattr(supervisor, "status", lambda: [{"id": "worker"}])
    peer = snapshot("peer-only")
    calls = []

    async def fetch(slug, path):
        calls.append(path)
        query = parse_qs(urlparse(path).query)
        if "by_agent" in query:
            windows = json.loads(query["attribution_windows_json"][0])
            return {**peer, "attribution": [{**w, "tokens": 40} for w in windows]}
        return peer

    monkeypatch.setattr(telemetry_routes, "_fetch_member_json", fetch)
    monkeypatch.setattr(auth, "_BEARER", ["operator-secret"])
    monkeypatch.setattr(auth, "_FEDERATION", ["federation-secret"])
    monkeypatch.setattr(auth, "_FLEET", [None])
    monkeypatch.setattr(auth, "_API_KEY", [""])
    app = FastAPI()
    register_usage_routes(app)
    app.add_middleware(auth.A2AAuthMiddleware)
    client = TestClient(app)
    path = "/api/usage/fleet?by_agent=true"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer federation-secret"}).status_code == 403
    assert not calls
    response = client.get(path, headers={"Authorization": "Bearer operator-secret"})
    assert response.status_code == 200
    rows = response.json()["accounts"][0]["by_agent"][0]["members"]
    assert sum(r["share_pct"] for r in rows) == 100
    assert rows[-1] == {"member": "worker", "tokens": 40, "share_pct": 100}


def test_account_helper_failure_never_raises(monkeypatch):
    monkeypatch.setattr(accounts, "account_for_delegate", lambda _: 1 / 0)
    assert delegation_usage.acp_usage("coder", {"inputTokens": 20}) == {}


async def test_acp_prompt_passes_captured_usage_to_recording(store, monkeypatch):
    from plugins.coding_agent.acp_client import AcpClient

    monkeypatch.setattr(accounts, "_configured_delegates", lambda: [
        {"name": "coder", "command": "claude-agent-acp"},
    ])
    client = AcpClient("unused", [], name="coder", cwd=".")

    async def prompt_locked(*args, **kwargs):
        client._turn_usage = {"inputTokens": 11, "outputTokens": 7}
        client._turn_session_id = "s"
        return "implemented"

    monkeypatch.setattr(client, "_prompt_locked", prompt_locked)
    assert await client.prompt("implement") == "implemented"
    row = store.recent()[0]
    assert row["total_tokens"] == 18
    assert row["provider_id"] == "anthropic-oauth"
    assert row["cost_usd"] == 0


def test_unknown_identity_and_windows_remain_unknown(store, monkeypatch):
    monkeypatch.setattr(oauth, "peek_codex_access_token", lambda: SimpleNamespace(account_id=None))
    assert delegation_usage.acp_usage({"command": "codex-acp"}, {"inputTokens": 7}) == {}
    body = snapshot()
    body["accounts"][0]["stale"] = True
    assert usage.attribution_windows(body["accounts"]) == []
    body["accounts"][0]["stale"] = False
    body["accounts"][0]["windows"][0]["window_seconds"] = None
    assert usage.attribution_windows(body["accounts"]) == []
    assert usage.local_attribution([{"account_key": "shared"}]) == []


def test_local_route_returns_counts_without_polling_in_cache_mode(store, monkeypatch):
    body = snapshot()
    store.record({"task_id": "t", "account_key": "shared", "total_tokens": 45,
                  "ended_at": datetime.now(UTC).isoformat()})
    calls = []

    def read(*args, **kwargs):
        calls.append(kwargs)
        return body

    monkeypatch.setattr(usage, "snapshot", read)
    app = FastAPI()
    register_usage_routes(app)
    response = TestClient(app).get("/api/usage?by_agent=true&cache_only=true")
    assert response.status_code == 200
    assert response.json()["accounts"][0]["by_agent"][0]["attributed_tokens"] == 45
    assert calls[0]["cache_only"] is True


def test_gateway_override_is_not_attributed_to_native_default(store, monkeypatch):
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig.from_dict({
        "model": {"provider": "anthropic-oauth", "name": "claude-sonnet-4-6"},
    }))
    record_turn(task_id="gateway", session_id="", state="completed", models=["gpt"],
                requested_model="gateway:protolabs/smart", usage={"input_tokens": 10},
                publish_usage_event=False)
    assert store.recent()[0]["account_key"] == ""


def test_by_agent_preserves_stale_warning_behavior(monkeypatch, capsys):
    host = snapshot()
    host["accounts"][0]["stale"] = True
    host["accounts"][0]["windows"][0]["used_pct"] = 99
    body = usage.merge_fleet_usage(host, {})
    usage.merge_attribution(body["accounts"], [], {})
    client = SimpleNamespace(url="http://fake", member_get=lambda *a, **kw: body, close=lambda: None)
    monkeypatch.setattr(usage_cli.deckhub, "connect", lambda **kw: SimpleNamespace(client=client))
    assert usage_cli.run_usage_cli(["--fleet", "--by-agent", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["accounts"][0]["stale"] is True
    assert result["warn_hit"] is False
