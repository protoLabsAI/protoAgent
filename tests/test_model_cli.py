"""Tests for `protoagent model` (ADR 0075 D5, `graph/model_cli.py`).

`model use` is the load-bearing verb — the non-interactive one-liner that points
protoAgent at a local / OpenAI-compatible endpoint (and the copy-paste target for
HuggingFace's "Use this model" local-app snippet)."""

from __future__ import annotations

import yaml

from graph import model_cli
from graph.model_cli import _normalize_model_id, run_model_cli


def _patch_config_path(monkeypatch, path, initial="{}\n"):
    """Isolate BOTH config files. The secrets overlay matters even for reads: `model use`
    consults it before deciding a remote endpoint has no credential, and `--key` writes to
    it — an unisolated run would read and write the developer's real secrets.yaml."""
    path.write_text(initial, encoding="utf-8")
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: path)
    secrets = path.parent / "secrets.yaml"
    monkeypatch.setattr("graph.config_io.secrets_yaml_path", lambda: secrets)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return secrets


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


def test_use_explicit_key_goes_to_the_secrets_overlay(monkeypatch, tmp_path):
    cfg = tmp_path / "c.yaml"
    secrets = _patch_config_path(monkeypatch, cfg)
    run_model_cli(["use", "--base-url", "http://x/v1", "--model", "m", "--key", "sk-explicit"])

    assert "sk-explicit" not in cfg.read_text()  # a real key never lands in the tracked YAML
    assert yaml.safe_load(secrets.read_text())["model"]["api_key"] == "sk-explicit"


# ── #2579: never invent a credential for an endpoint that will reject it ─────


def test_use_does_not_invent_a_key_for_a_remote_gateway(monkeypatch, tmp_path, capsys):
    """The bug: `model use` wrote api_key "local" whatever the endpoint was, so `setup`
    saw a non-blank key, declared setup complete, and the agent 401'd on every turn."""
    cfg = tmp_path / "c.yaml"
    _patch_config_path(monkeypatch, cfg)

    rc = run_model_cli(["use", "--base-url", "http://ava:4000/v1", "--model", "protolabs/reasoning"])

    doc = yaml.safe_load(cfg.read_text())
    assert rc == 0
    assert doc["model"]["api_base"] == "http://ava:4000/v1"  # the endpoint is still written
    assert "api_key" not in doc["model"]  # but no credential is conjured
    assert "no API key configured" in capsys.readouterr().err  # and it says so


def test_use_still_writes_the_placeholder_for_a_loopback_endpoint(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    _patch_config_path(monkeypatch, cfg)

    run_model_cli(["use", "--base-url", "http://127.0.0.1:1234/v1", "--model", "gemma"])

    assert yaml.safe_load(cfg.read_text())["model"]["api_key"] == model_cli._LOCAL_KEY_PLACEHOLDER
    assert "no API key configured" not in capsys.readouterr().err  # keyless local is fine


def test_use_clears_a_stale_placeholder_when_repointed_at_a_remote(monkeypatch, tmp_path):
    """Heals an instance already broken by the old behavior, instead of leaving the
    invented key to keep masquerading as a configured credential."""
    cfg = tmp_path / "c.yaml"
    _patch_config_path(monkeypatch, cfg, "model:\n  api_key: local\n")

    run_model_cli(["use", "--base-url", "https://gw.example.com/v1", "--model", "m"])

    assert "api_key" not in yaml.safe_load(cfg.read_text())["model"]


def test_use_is_quiet_when_a_key_is_resolvable_from_the_overlay(monkeypatch, tmp_path, capsys):
    from graph import config_io

    cfg = tmp_path / "c.yaml"
    _patch_config_path(monkeypatch, cfg)
    config_io.save_secrets({"model": {"api_key": "sk-stored"}})

    run_model_cli(["use", "--base-url", "http://ava:4000/v1", "--model", "m"])

    assert "no API key configured" not in capsys.readouterr().err


def test_use_is_quiet_when_openai_api_key_is_exported(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "c.yaml"
    _patch_config_path(monkeypatch, cfg)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")

    run_model_cli(["use", "--base-url", "http://ava:4000/v1", "--model", "m"])

    assert "no API key configured" not in capsys.readouterr().err


def test_loopback_detection(monkeypatch):
    for url in ("http://127.0.0.1:8080/v1", "http://localhost:1234/v1", "http://[::1]:8080/v1"):
        assert model_cli._is_loopback_endpoint(url), url
    for url in ("http://ava:4000/v1", "https://api.example.com/v1", "http://192.168.1.9:4000/v1", "not a url"):
        assert not model_cli._is_loopback_endpoint(url), url


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
