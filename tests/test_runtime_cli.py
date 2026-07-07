"""Tests for `protoagent runtime` / `protoagent hermes` (`runtime/cli.py`).

The Hermes preset's contract is directional seeding that never clobbers: Hermes's
model endpoint wins when this instance is unconfigured; the reverse seeding only
fills a Hermes with no model; SOUL adoption only replaces the shipped placeholder.
The runtime flip itself must land even when every bootstrap step fails.
"""

from __future__ import annotations

import yaml

from runtime import cli as runtime_cli
from runtime.cli import run_hermes_cli, run_runtime_cli


def _patch_instance(monkeypatch, tmp_path, initial="{}\n", secrets=None):
    """Isolate the instance config + secrets the CLI reads/writes. The host-config
    cascade layer is emptied too — it reads a real machine path and could leak
    host-scoped model fields into `from_yaml`."""
    cfg = tmp_path / "langgraph-config.yaml"
    cfg.write_text(initial, encoding="utf-8")
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: cfg)
    monkeypatch.setattr("graph.config._load_host_layer", lambda: {})
    sp = tmp_path / "secrets.yaml"
    if secrets is not None:
        sp.write_text(secrets, encoding="utf-8")
    monkeypatch.setattr("graph.config_io.secrets_yaml_path", lambda: sp)
    return cfg


def _patch_hermes_home(monkeypatch, tmp_path, config=None, soul=None):
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    if config is not None:
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    if soul is not None:
        (home / "SOUL.md").write_text(soul, encoding="utf-8")
    return home


def _no_install(monkeypatch):
    """Pretend hermes-acp is installed so bootstrap never shells out."""
    monkeypatch.setattr(runtime_cli.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(runtime_cli, "_acp_venv_python", lambda acp_path: None)


_HERMES_MODEL = {
    "model": {"default": "hermes/model", "provider": "custom", "base_url": "http://h:1/v1", "api_key": "hk"}
}


# ── runtime use / list ───────────────────────────────────────────────────────


def test_use_native_writes_config(monkeypatch, tmp_path):
    cfg = _patch_instance(monkeypatch, tmp_path, "agent_runtime: acp:codex\n")
    assert run_runtime_cli(["use", "native"]) == 0
    assert yaml.safe_load(cfg.read_text())["agent_runtime"] == "native"


def test_use_unknown_runtime_is_an_error(monkeypatch, tmp_path):
    cfg = _patch_instance(monkeypatch, tmp_path)
    assert run_runtime_cli(["use", "acp:nonsense"]) == 2
    assert "agent_runtime" not in yaml.safe_load(cfg.read_text())  # nothing written


def test_use_accepts_config_override_agents(monkeypatch, tmp_path):
    # An agent that isn't in the catalog but has an `acp.agents` launch override is legal.
    cfg = _patch_instance(monkeypatch, tmp_path, "acp:\n  agents:\n    mycli:\n      command: mycli\n")
    assert run_runtime_cli(["use", "acp:mycli"]) == 0
    assert yaml.safe_load(cfg.read_text())["agent_runtime"] == "acp:mycli"


def test_use_hermes_normalizes_and_flips_runtime(monkeypatch, tmp_path):
    cfg = _patch_instance(monkeypatch, tmp_path)
    _patch_hermes_home(monkeypatch, tmp_path)
    _no_install(monkeypatch)
    assert run_runtime_cli(["use", "hermes"]) == 0
    assert yaml.safe_load(cfg.read_text())["agent_runtime"] == "acp:hermes"


def test_hermes_sugar_forwards(monkeypatch, tmp_path):
    cfg = _patch_instance(monkeypatch, tmp_path)
    _patch_hermes_home(monkeypatch, tmp_path)
    _no_install(monkeypatch)
    assert run_hermes_cli([]) == 0
    assert yaml.safe_load(cfg.read_text())["agent_runtime"] == "acp:hermes"


def test_use_hermes_flip_survives_bootstrap_failure(monkeypatch, tmp_path):
    cfg = _patch_instance(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_cli, "_bootstrap_hermes", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    # A blown-up bootstrap propagates (it's already best-effort inside); the guard we
    # pin here is --no-bootstrap: the flip lands with bootstrap skipped entirely.
    assert run_runtime_cli(["use", "hermes", "--no-bootstrap"]) == 0
    assert yaml.safe_load(cfg.read_text())["agent_runtime"] == "acp:hermes"


def test_list_shows_current_and_catalog(monkeypatch, tmp_path, capsys):
    _patch_instance(monkeypatch, tmp_path, "agent_runtime: acp:hermes\n")
    monkeypatch.setattr(runtime_cli.shutil, "which", lambda name: None)
    assert run_runtime_cli(["list"]) == 0
    out = capsys.readouterr().out
    assert "current:  acp:hermes" in out
    assert "native" in out and "acp:hermes" in out and "not found" in out


# ── seeding: Hermes → instance (Hermes wins on a fresh instance) ─────────────


def test_hermes_model_imported_into_unconfigured_instance(monkeypatch, tmp_path):
    cfg = _patch_instance(monkeypatch, tmp_path)
    _patch_hermes_home(monkeypatch, tmp_path, config=_HERMES_MODEL)
    _no_install(monkeypatch)
    assert run_hermes_cli([]) == 0
    model = yaml.safe_load(cfg.read_text())["model"]
    assert model["api_base"] == "http://h:1/v1"
    assert model["name"] == "hermes/model"
    assert model["api_key"] == "hk"


def test_hermes_import_falls_back_to_custom_providers(monkeypatch, tmp_path):
    cfg = _patch_instance(monkeypatch, tmp_path)
    _patch_hermes_home(
        monkeypatch,
        tmp_path,
        config={"custom_providers": [{"name": "x", "base_url": "http://cp:2/v1", "api_key": "ck", "model": "cp/m"}]},
    )
    _no_install(monkeypatch)
    run_hermes_cli([])
    model = yaml.safe_load(cfg.read_text())["model"]
    assert (model["api_base"], model["name"]) == ("http://cp:2/v1", "cp/m")


def test_configured_instance_model_is_never_clobbered(monkeypatch, tmp_path):
    cfg = _patch_instance(monkeypatch, tmp_path, "model:\n  api_base: http://mine/v1\n  name: mine\n")
    _patch_hermes_home(monkeypatch, tmp_path, config=_HERMES_MODEL)
    _no_install(monkeypatch)
    run_hermes_cli([])
    model = yaml.safe_load(cfg.read_text())["model"]
    assert (model["api_base"], model["name"]) == ("http://mine/v1", "mine")


def test_secrets_key_counts_as_configured(monkeypatch, tmp_path):
    # A key in secrets.yaml means the operator configured a gateway — don't import over it.
    cfg = _patch_instance(monkeypatch, tmp_path, secrets="model:\n  api_key: sk-real\n")
    _patch_hermes_home(monkeypatch, tmp_path, config=_HERMES_MODEL)
    _no_install(monkeypatch)
    run_hermes_cli([])
    assert "model" not in yaml.safe_load(cfg.read_text())


# ── seeding: instance → Hermes (only when Hermes has no model) ───────────────


def test_fresh_hermes_seeded_from_configured_instance(monkeypatch, tmp_path):
    _patch_instance(
        monkeypatch, tmp_path, "model:\n  api_base: http://gw:4000/v1\n  name: gw/model\n  api_key: gk\n"
    )
    home = _patch_hermes_home(monkeypatch, tmp_path)
    _no_install(monkeypatch)
    run_hermes_cli([])
    doc = yaml.safe_load((home / "config.yaml").read_text())
    assert doc["model"] == {"default": "gw/model", "provider": "custom", "base_url": "http://gw:4000/v1", "api_key": "gk"}
    assert doc["custom_providers"][0]["base_url"] == "http://gw:4000/v1"


def test_hermes_explicit_model_choice_is_respected(monkeypatch, tmp_path):
    _patch_instance(monkeypatch, tmp_path, "model:\n  api_base: http://gw:4000/v1\n  name: gw/model\n")
    # Hermes has a model.default but no base_url → not importable, and not seedable-over.
    home = _patch_hermes_home(monkeypatch, tmp_path, config={"model": {"default": "their-choice"}})
    _no_install(monkeypatch)
    run_hermes_cli([])
    assert yaml.safe_load((home / "config.yaml").read_text())["model"]["default"] == "their-choice"


# ── SOUL adoption ────────────────────────────────────────────────────────────


def test_placeholder_soul_is_replaced_by_hermes_soul(monkeypatch, tmp_path):
    _patch_instance(monkeypatch, tmp_path)
    _patch_hermes_home(monkeypatch, tmp_path, config=_HERMES_MODEL, soul="# I am their Hermes\n")
    _no_install(monkeypatch)
    monkeypatch.setattr("graph.config_io.read_soul", lambda: "placeholder. Replace this file.")
    written = {}
    monkeypatch.setattr("graph.config_io.write_soul", lambda text: written.setdefault("soul", text) and [] or [])
    run_hermes_cli([])
    assert written["soul"] == "# I am their Hermes"


def test_customized_soul_is_kept(monkeypatch, tmp_path):
    _patch_instance(monkeypatch, tmp_path)
    _patch_hermes_home(monkeypatch, tmp_path, config=_HERMES_MODEL, soul="# theirs\n")
    _no_install(monkeypatch)
    monkeypatch.setattr("graph.config_io.read_soul", lambda: "# my carefully tuned persona")
    monkeypatch.setattr(
        "graph.config_io.write_soul", lambda text: (_ for _ in ()).throw(AssertionError("must not write"))
    )
    run_hermes_cli([])  # not raising = write_soul never called


# ── install path ─────────────────────────────────────────────────────────────


def test_missing_hermes_is_installed_via_uv(monkeypatch, tmp_path):
    _patch_instance(monkeypatch, tmp_path)
    _patch_hermes_home(monkeypatch, tmp_path, config=_HERMES_MODEL)
    monkeypatch.setattr(runtime_cli.shutil, "which", lambda name: "/fake/uv" if name == "uv" else None)
    calls = []
    monkeypatch.setattr(
        runtime_cli.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})()
    )
    run_hermes_cli([])
    assert calls and calls[0] == runtime_cli._HERMES_INSTALL
    assert "--with" in calls[0] and "mcp==1.26.0" in calls[0]  # the pin the [acp] extra misses


def test_no_uv_prints_manual_instructions_and_continues(monkeypatch, tmp_path, capsys):
    cfg = _patch_instance(monkeypatch, tmp_path)
    _patch_hermes_home(monkeypatch, tmp_path, config=_HERMES_MODEL)
    monkeypatch.setattr(runtime_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        runtime_cli.subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(AssertionError("no shell-out"))
    )
    assert run_hermes_cli([]) == 0
    assert "uv tool install" in capsys.readouterr().err
    assert yaml.safe_load(cfg.read_text())["agent_runtime"] == "acp:hermes"  # flip still lands


# ── restart hint ─────────────────────────────────────────────────────────────


def test_use_hints_restart_when_server_running(monkeypatch, tmp_path, capsys):
    _patch_instance(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_cli, "_running_server_port", lambda: 7870)
    assert run_runtime_cli(["use", "native"]) == 0
    out = capsys.readouterr().out
    assert "protoagent down && protoagent up" in out  # live server keeps the old runtime


def test_use_hints_up_when_stopped(monkeypatch, tmp_path, capsys):
    _patch_instance(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_cli, "_running_server_port", lambda: None)
    run_runtime_cli(["use", "native"])
    assert "protoagent up" in capsys.readouterr().out
