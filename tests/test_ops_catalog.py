"""ops catalog (ADR 0075 D2) — load_all + GET /api/operations + the operations/config CLI."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_load_all_registers_every_op_family():
    from ops import load_all

    names = set(load_all())
    assert {
        "knowledge.ingest",
        "knowledge.ingest_preview",
        "plugins.install_and_activate",
        "config.set",
        "config.get",
        "fleet.up",
        "fleet.down",
        "fleet.status",
    } <= names


def test_operations_route_lists_sorted_catalog():
    from operator_api.operations_routes import register_operations_routes

    app = FastAPI()
    register_operations_routes(app)
    body = TestClient(app).get("/api/operations").json()
    by_name = {o["name"]: o for o in body["operations"]}
    assert by_name["config.set"]["mutates"] is True
    assert by_name["config.get"]["mutates"] is False and by_name["fleet.status"]["mutates"] is False
    assert by_name["knowledge.ingest"]["summary"]
    names = [o["name"] for o in body["operations"]]
    assert names == sorted(names)


def test_operations_cli_prints_catalog(capsys):
    from ops.cli import run_operations_cli

    assert run_operations_cli([]) == 0
    out = capsys.readouterr().out
    assert "config.set" in out and "fleet.status" in out

    assert run_operations_cli(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any(o["name"] == "plugins.install_and_activate" for o in data)


def test_config_cli_set_writes_disk(monkeypatch, capsys):
    import graph.config_io as cio
    from graph.config_explain import run_config_cli

    captured: dict = {}
    monkeypatch.setattr(cio, "config_yaml_path", lambda: "cfg.yaml")
    monkeypatch.setattr(cio, "load_yaml_doc", lambda p=None: {})
    monkeypatch.setattr(cio, "apply_updates_to_yaml", lambda doc, updates: {**doc, **updates})
    monkeypatch.setattr(cio, "save_yaml_doc", lambda doc, p=None: captured.update(doc=doc))

    assert run_config_cli(["set", "fleet.mdns_enabled=false", "server.port=7871"]) == 0
    assert captured["doc"]["fleet"]["mdns_enabled"] is False  # JSON-typed + nested
    assert captured["doc"]["server"]["port"] == 7871


def test_config_cli_get_reads_disk(monkeypatch, capsys):
    import graph.config_io as cio
    from graph.config_explain import run_config_cli

    monkeypatch.setattr(cio, "config_yaml_path", lambda: "cfg.yaml")
    monkeypatch.setattr(cio, "load_yaml_doc", lambda p=None: {"server": {"port": 7870}})
    assert run_config_cli(["get", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"server": {"port": 7870}}


def test_config_cli_get_yaml_renders_from_ruamel(monkeypatch, capsys):
    """Regression: `protoagent config get` (default YAML, no --json) crashed with
    yaml.representer.RepresenterError when the on-disk doc loaded as a ruamel CommentedMap."""
    import io

    import pytest

    yaml_rt = pytest.importorskip("ruamel.yaml")
    import graph.config_io as cio
    from graph.config_explain import run_config_cli

    cm = yaml_rt.YAML(typ="rt").load(io.StringIO("server:\n  port: 7870\n"))
    monkeypatch.setattr(cio, "config_yaml_path", lambda: "cfg.yaml")
    monkeypatch.setattr(cio, "load_yaml_doc", lambda p=None: cm)
    assert run_config_cli(["get"]) == 0  # was exit 1 + a traceback
    assert "port: 7870" in capsys.readouterr().out


def test_config_cli_set_rejects_bad_pair(capsys):
    from graph.config_explain import run_config_cli

    assert run_config_cli(["set", "noequalssign"]) == 2
    assert "expected key=value" in capsys.readouterr().err
