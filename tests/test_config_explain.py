"""``config explain`` diagnostic — the read-only "where did my config/key go?"
builder shared by the CLI (`python -m server config explain`) and the operator
route (`GET /api/config/explain`).

Covers: the builder reports the env-resolved roots/id; secrets are redacted (never
echoed); the cascade lists a known field with a plausible source; the CLI renderer
runs; and the API route returns the expected top-level shape.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.paths import reset_instance_paths


def _isolate(monkeypatch, tmp_path, instance="alpha"):
    """Point this instance at an empty tmp box so the builder reads no real
    on-disk config, then re-resolve the cached InstancePaths singleton."""
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(box))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", instance)
    monkeypatch.delenv("PROTOAGENT_HOME", raising=False)
    # conftest pins PROTOAGENT_HOST_CONFIG to an absent path for cascade isolation;
    # clear it so host_config resolves box-relatively (the real two-tier default).
    monkeypatch.delenv("PROTOAGENT_HOST_CONFIG", raising=False)
    reset_instance_paths()
    return box


def test_explain_reports_env_resolved_roots(monkeypatch, tmp_path):
    from graph.config_explain import build_config_explain

    box = _isolate(monkeypatch, tmp_path, instance="alpha")
    data = build_config_explain()

    assert data["instance_id"] == "alpha"
    assert data["box_root"] == str(box)
    assert data["instance_root"] == str(box / "alpha")
    # Every resolved path nests under the instance root (or the box, for shared tiers).
    assert data["paths"]["config_yaml"] == str(box / "alpha" / "config" / "langgraph-config.yaml")
    assert data["paths"]["host_config"] == str(box / "host-config.yaml")


def test_secret_values_are_redacted(monkeypatch, tmp_path):
    from graph.config import LangGraphConfig
    from graph.config_explain import build_config_explain

    _isolate(monkeypatch, tmp_path)
    secret = "sk-SUPERSECRET-must-not-leak-123"
    cfg = LangGraphConfig(api_key=secret, auth_token="tok-SECRET-also-hidden")
    data = build_config_explain(cfg)

    # The raw secret never appears anywhere in the serialized payload.
    assert secret not in json.dumps(data)
    assert "tok-SECRET-also-hidden" not in json.dumps(data)
    # ...but the diagnostic still tells you the key IS configured.
    api_key = next(c for c in data["cascade"] if c["key"] == "model.api_key")
    assert api_key["value"] == "<set>"
    token = next(c for c in data["cascade"] if c["key"] == "auth.token")
    assert token["value"] == "<set>"


def test_unset_secret_marks_unset(monkeypatch, tmp_path):
    from graph.config import LangGraphConfig
    from graph.config_explain import build_config_explain

    _isolate(monkeypatch, tmp_path)
    data = build_config_explain(LangGraphConfig(api_key="", auth_token=""))
    api_key = next(c for c in data["cascade"] if c["key"] == "model.api_key")
    assert api_key["value"] == "<unset>"


def test_cascade_lists_known_field_with_source(monkeypatch, tmp_path):
    from graph.config_explain import build_config_explain

    _isolate(monkeypatch, tmp_path)
    data = build_config_explain()
    by_key = {c["key"]: c for c in data["cascade"]}

    # A known host-scoped field is present, with a plausible (default — no agent
    # leaf, no host file in the isolated box) source and its declared scope.
    assert "model.name" in by_key
    assert by_key["model.name"]["scope"] == "host"
    assert by_key["model.name"]["source"] in {"agent", "host", "default"}
    # And a known agent-scoped field.
    assert by_key["model.temperature"]["scope"] == "agent"


def test_cli_renderer_and_smoke(monkeypatch, tmp_path, capsys):
    from graph.config_explain import build_config_explain, render_config_explain, run_config_cli

    _isolate(monkeypatch, tmp_path, instance="beta")
    text = render_config_explain(build_config_explain())
    assert "Instance" in text
    assert "id:             beta" in text
    assert "Cascade" in text
    assert "model.name" in text

    rc = run_config_cli(["explain"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "beta" in out and "Paths" in out


def test_cli_json_output(monkeypatch, tmp_path, capsys):
    from graph.config_explain import run_config_cli

    _isolate(monkeypatch, tmp_path, instance="gamma")
    rc = run_config_cli(["explain", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["instance_id"] == "gamma"
    assert {"instance_id", "box_root", "instance_root", "app_root", "paths", "cascade"} <= payload.keys()


def test_api_route_returns_expected_keys(monkeypatch, tmp_path):
    from operator_api.config_routes import register_config_routes

    _isolate(monkeypatch, tmp_path, instance="delta")
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)

    app = FastAPI()
    register_config_routes(app)
    resp = TestClient(app).get("/api/config/explain")
    assert resp.status_code == 200
    body = resp.json()
    assert {"instance_id", "box_root", "instance_root", "app_root", "paths", "cascade"} <= body.keys()
    assert body["instance_id"] == "delta"
    assert isinstance(body["cascade"], list) and body["cascade"]
