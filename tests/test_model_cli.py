"""Tests for `protoagent model` (ADR 0075 D5, `graph/model_cli.py`).

`model use` is the load-bearing verb — the non-interactive one-liner that points
protoAgent at a local / OpenAI-compatible endpoint (and the copy-paste target for
HuggingFace's "Use this model" local-app snippet)."""

from __future__ import annotations

import yaml

from graph import model_cli
from graph.model_cli import _normalize_model_id, run_model_cli


def _patch_config_path(monkeypatch, path, initial="{}\n"):
    path.write_text(initial, encoding="utf-8")
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: path)


def test_use_writes_base_model_and_placeholder_key(monkeypatch, tmp_path):
    cfg = tmp_path / "langgraph-config.yaml"
    _patch_config_path(monkeypatch, cfg, "model:\n  name: old\n")
    rc = run_model_cli(["use", "--base-url", "http://127.0.0.1:8080/v1", "--model", "qwen2.5"])
    assert rc == 0
    doc = yaml.safe_load(cfg.read_text())
    assert doc["model"]["api_base"] == "http://127.0.0.1:8080/v1"
    assert doc["model"]["name"] == "qwen2.5"
    assert doc["model"]["provider"] == "openai"
    assert doc["model"]["api_key"]  # a non-empty placeholder so the OpenAI client constructs


def test_use_keeps_an_existing_real_key(monkeypatch, tmp_path):
    cfg = tmp_path / "c.yaml"
    _patch_config_path(monkeypatch, cfg, "model:\n  api_key: real-gateway-key\n")
    run_model_cli(["use", "--base-url", "http://x/v1", "--model", "m"])
    assert yaml.safe_load(cfg.read_text())["model"]["api_key"] == "real-gateway-key"  # not clobbered


def test_use_explicit_key_wins(monkeypatch, tmp_path):
    cfg = tmp_path / "c.yaml"
    _patch_config_path(monkeypatch, cfg)
    run_model_cli(["use", "--base-url", "http://x/v1", "--model", "m", "--key", "sk-explicit"])
    assert yaml.safe_load(cfg.read_text())["model"]["api_key"] == "sk-explicit"


def test_hf_quant_placeholder_is_stripped():
    # HF passes the literal :{{QUANT_TAG}} when no GGUF file is chosen — the server
    # defaults its own quant, so we strip it; a real :quant suffix is kept.
    assert _normalize_model_id("unsloth/qwen-GGUF:{{QUANT_TAG}}") == "unsloth/qwen-GGUF"
    assert _normalize_model_id("unsloth/qwen-GGUF:Q4_K_M") == "unsloth/qwen-GGUF:Q4_K_M"


def test_use_rejects_model_that_normalizes_to_empty(monkeypatch, tmp_path):
    cfg = tmp_path / "c.yaml"
    _patch_config_path(monkeypatch, cfg)
    rc = run_model_cli(["use", "--base-url", "http://x/v1", "--model", ":{{QUANT_TAG}}"])
    assert rc == 2  # nothing usable to point at


def test_discover_parses_openai_models(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "qwen2.5"}, {"id": "llama3"}]}

    def _fake_get(url, timeout=None):
        if "11434" in url:  # only Ollama is "up"
            return _Resp()
        raise RuntimeError("connection refused")

    monkeypatch.setattr("httpx.get", _fake_get)
    found = model_cli._discover()
    assert len(found) == 1
    assert found[0]["name"] == "ollama" and found[0]["models"] == ["qwen2.5", "llama3"]


def test_discover_none_reachable_is_empty(monkeypatch):
    def _fail(url, timeout=None):
        raise RuntimeError("refused")

    monkeypatch.setattr("httpx.get", _fail)
    assert model_cli._discover() == []


def test_dispatch_routes_model_subcommand(monkeypatch):
    from server import cli

    seen = {}
    monkeypatch.setattr(model_cli, "run_model_cli", lambda argv: seen.update(argv=argv) or 0)
    assert cli.dispatch(["model", "use", "--model", "x"]) == 0
    assert seen["argv"] == ["use", "--model", "x"]


def test_model_appears_in_help(capsys):
    from server import cli

    cli.main(["--help"])
    assert "model" in capsys.readouterr().out
