"""The guarded agent-owned config writer (`tools.self_config_enabled`, default off).

The tool exists so an agent can retune itself — a model slot, a plugin's coder — instead of
handing its operator a YAML diff and stopping. The whole design question is the fence: a
config tool that can reach `filesystem.allow_run` or `plugins.enabled` is not a config tool,
it is a privilege-escalation tool, because those keys decide what the agent is ALLOWED to
do. So most of this suite is about what the tool refuses.
"""

from __future__ import annotations

import asyncio

import pytest

from tools import lg_tools


def _tool():
    (t,) = lg_tools._build_config_editor_tool()
    return t


def _call(tool, updates):
    return asyncio.run(tool.ainvoke({"updates": updates}))


# ── the fence ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "filesystem.allow_run",      # shell execution
        "filesystem.projects",       # the ADR 0007 project fence
        "operator.allowed_dirs",     # reachable directories
        "egress.enabled",            # ADR 0008 network fence
        "plugins.enabled",           # runs code in-process AS the agent
        "auth.token",                # the operator API's gate
        "mcp.servers",               # out-of-process capability
        "soul.self_edit_enabled",    # persona has its own guarded path
        "self_improvement.enabled",  # no self-enabling automatic mutation
        "tools.self_config_enabled", # no widening its own fence
        "tools.disabled",            # ...or re-enabling a tool the operator removed
        "delegates",                 # each entry is an executable the host spawns
        "runtime.acp",               # per-agent command overrides, same spawn path
        "acp.agents",
    ],
)
def test_refuses_the_trust_surface(key):
    out = _call(_tool(), {key: True})
    assert out.startswith("Refused:"), f"{key} was not refused: {out}"
    assert key.split(".")[0] in out


@pytest.mark.parametrize(
    "key",
    [
        "some_plugin.command",       # a plugin section can't be enumerated in advance...
        "another.nested.executable", # ...so the leaf name is what catches it
        "x.args",
        "y.interpreter",
        "z.BINARY",                  # case-insensitive
    ],
)
def test_refuses_defining_an_executable_in_any_section(key):
    """Plugin sections are named after their plugin, so the section denylist can't cover
    them. An agent may CHOOSE a provisioned executable by name; defining one is the
    operator's. This is the guard that stopped `delegates[].command` being an ACE hole."""
    out = _call(_tool(), {key: "/bin/sh"})
    assert out.startswith("Refused:") and "program to run" in out


def test_choosing_a_provisioned_coder_by_name_is_allowed(monkeypatch):
    """The motivating case: repointing a board at an existing delegate must still work —
    the fence blocks defining executables, not selecting among them."""
    seen = {}
    from graph.plugins import host

    monkeypatch.setattr(host.HOST, "apply_settings", lambda p: (seen.update(p), (True, []))[1], raising=False)

    out = _call(_tool(), {"project_board.coder": "proto"})

    assert "Applied:" in out
    assert seen == {"project_board": {"coder": "proto"}}


def test_a_denied_key_refuses_the_WHOLE_write(monkeypatch):
    """No partial application. Otherwise an agent could smuggle a fence change through by
    batching it with legitimate keys and relying on the good ones landing."""
    applied = []
    from graph.plugins import host

    def _apply(patch):
        applied.append(patch)
        return True, []

    monkeypatch.setattr(host.HOST, "apply_settings", _apply, raising=False)

    out = _call(_tool(), {"project_board.coder": "proto", "filesystem.allow_run": True})

    assert out.startswith("Refused:")
    assert applied == [], "a batch containing a denied key must not apply ANY of it"


def test_refuses_secrets(monkeypatch):
    """The op would faithfully route a secret into secrets.yaml — correct for the CLI and the
    console, wrong here: it would put a live credential in the turn transcript."""
    from graph.plugins import host

    monkeypatch.setattr(host.HOST, "apply_settings", lambda patch: (True, []), raising=False)

    out = _call(_tool(), {"model.api_key": "sk-live-123"})

    assert out.startswith("Refused:")
    assert "secret" in out.lower()
    assert "sk-live-123" not in out, "the tool must not echo the credential back"


# ── the happy path ────────────────────────────────────────────────────────────────────


def test_applies_operational_keys_nested(monkeypatch):
    """Operational keys go through, nested the way the write path expects."""
    seen = {}
    from graph.plugins import host

    def _apply(patch):
        seen.update(patch)
        return True, ["reloaded"]

    monkeypatch.setattr(host.HOST, "apply_settings", _apply, raising=False)

    out = _call(_tool(), {"project_board.coder": "proto", "routing.aux_model": "claude-opus-4-6"})

    assert seen == {"project_board": {"coder": "proto"}, "routing": {"aux_model": "claude-opus-4-6"}}
    assert "Applied:" in out and "project_board.coder" in out


def test_reports_a_rejected_write_instead_of_claiming_success(monkeypatch):
    from graph.plugins import host

    monkeypatch.setattr(host.HOST, "apply_settings", lambda patch: (False, ["bad value"]), raising=False)

    out = _call(_tool(), {"routing.aux_model": "nonsense"})

    assert out.startswith("Error:") and "bad value" in out


def test_no_host_is_an_error_not_a_silent_noop(monkeypatch):
    from graph.plugins import host

    monkeypatch.setattr(host.HOST, "apply_settings", None, raising=False)
    assert _call(_tool(), {"routing.aux_model": "x"}).startswith("Error:")


def test_rejects_an_empty_update():
    assert _call(_tool(), {}).startswith("Error:")


def test_rejects_blank_keys():
    assert _call(_tool(), {"   ": "x"}).startswith("Error:")


@pytest.mark.parametrize("bad", [None, "project_board.coder=proto", []])
def test_wrong_types_are_rejected_by_the_tool_schema(bad):
    """`updates: dict` is enforced by LangChain before the body runs — the model gets a
    validation error rather than a silent no-op, which is the outcome we want either way."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _call(_tool(), bad)


# ── the gate ──────────────────────────────────────────────────────────────────────────


def test_tool_is_absent_unless_the_operator_opts_in():
    """Off by default, and never in a subagent build — the same disposition as edit_soul."""
    default = {t.name for t in lg_tools.get_all_tools()}
    assert "set_config" not in default

    opted_in = {t.name for t in lg_tools.get_all_tools(self_config_enabled=True)}
    assert "set_config" in opted_in
