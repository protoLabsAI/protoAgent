"""#5322: a hub-spawned fleet member gets ``run_command`` by default.

The supervisor starts every member with ``--ui none``, and the headless tier defaults
``filesystem.allow_run`` OFF (#1849: nobody to answer the approval). A member is attended
through the hub console, so the supervisor marks its env with ``PROTOAGENT_FLEET_MEMBER``
and the default stays ON. Explicit config still wins; a plain headless instance is unchanged.
"""

from __future__ import annotations

import pytest

from graph.config import LangGraphConfig
from graph.config_load import FLEET_MEMBER_ENV, _default_filesystem_allow_run
from graph.fleet import supervisor
from graph.workspaces import manager


@pytest.fixture(autouse=True)
def _clean_tier_env(monkeypatch):
    """Neither the tier nor the marker may leak in from the shell (a coder running inside
    a hub member inherits both) or out to another test."""
    monkeypatch.delenv("PROTOAGENT_UI", raising=False)
    monkeypatch.delenv(FLEET_MEMBER_ENV, raising=False)
    monkeypatch.setattr("graph.config._load_host_layer", lambda *a, **k: {})
    monkeypatch.setattr("graph.config._resolve_plugin_config", lambda *a, **k: {})


def test_marker_name_is_the_documented_one():
    assert FLEET_MEMBER_ENV == "PROTOAGENT_FLEET_MEMBER"


def test_headless_without_marker_defaults_off(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_UI", "none")
    assert _default_filesystem_allow_run() is False
    assert LangGraphConfig.from_dict({}).filesystem_allow_run is False


def test_headless_fleet_member_defaults_on(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_UI", "none")
    monkeypatch.setenv(FLEET_MEMBER_ENV, "1")
    assert _default_filesystem_allow_run() is True
    assert LangGraphConfig.from_dict({}).filesystem_allow_run is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_falsy_marker_keeps_the_headless_default(monkeypatch, value):
    monkeypatch.setenv("PROTOAGENT_UI", "none")
    monkeypatch.setenv(FLEET_MEMBER_ENV, value)
    assert _default_filesystem_allow_run() is False


def test_explicit_false_wins_on_a_fleet_member(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_UI", "none")
    monkeypatch.setenv(FLEET_MEMBER_ENV, "1")
    cfg = LangGraphConfig.from_dict({"filesystem": {"allow_run": False}})
    assert cfg.filesystem_allow_run is False


def test_interactive_tier_is_unchanged_by_the_marker(monkeypatch):
    assert _default_filesystem_allow_run() is True
    monkeypatch.setenv(FLEET_MEMBER_ENV, "1")
    assert _default_filesystem_allow_run() is True


# ── the supervisor sets the marker on the member it spawns ────────────────────
def _env_capturing_fleet(tmp_path, monkeypatch):
    """Capture the env handed to Popen (mirrors tests/test_fleet_supervisor.py)."""
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    alive: set[int] = set()
    captured: dict = {}
    monkeypatch.setattr(supervisor, "_alive", lambda pid: int(pid) in alive if pid else False)

    class FakeProc:
        returncode = None

        def __init__(self, *a, **k):
            self.pid = 99001
            alive.add(99001)
            captured["env"] = k.get("env", {})
            captured["argv"] = a[0] if a else k.get("args")

        def poll(self):
            return None

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)
    monkeypatch.setattr(supervisor, "_port_listening", lambda port, timeout=0.25: True)
    monkeypatch.setattr(supervisor, "_port_held", lambda port: False)
    return captured


def test_supervisor_marks_the_spawned_member(tmp_path, monkeypatch):
    captured = _env_capturing_fleet(tmp_path, monkeypatch)
    manager.create("alpha", port=7890)

    supervisor.start("alpha")

    assert captured["env"][FLEET_MEMBER_ENV] == "1"
    # The member really is on the headless tier, which is why the marker is needed.
    argv = list(captured["argv"])
    assert argv[argv.index("--ui") + 1] == "none"


def test_supervisor_keeps_an_explicit_workspace_marker(tmp_path, monkeypatch):
    captured = _env_capturing_fleet(tmp_path, monkeypatch)
    manager.create("beta", port=7891)
    real_run_exec = manager.run_exec

    def run_exec_with_marker(wid, extra):
        env, argv = real_run_exec(wid, extra)
        env[FLEET_MEMBER_ENV] = "0"
        return env, argv

    monkeypatch.setattr(supervisor.manager, "run_exec", run_exec_with_marker)
    supervisor.start("beta")

    assert captured["env"][FLEET_MEMBER_ENV] == "0"  # explicit wins
