"""Coder launches must not inherit the live agent's identity (#4629)."""

import os

import pytest

from plugins import coding_agent
from plugins.coding_agent import acp_client
from plugins.delegates.adapters import ADAPTERS


pytestmark = pytest.mark.platform_sensitive

HOST_VARS = (
    "PROTOAGENT_HOME", "PROTOAGENT_INSTANCE", "PROTOAGENT_BOX_ROOT",
    "AGENT_NAME", "A2A_AUTH_TOKEN", "PROTOAGENT_FLEET_TOKEN",
)


@pytest.fixture
def host_env(monkeypatch):
    env = {name: "host-only" for name in HOST_VARS}
    env.update(PATH="/usr/bin", HOME="/home/operator", XDG_CONFIG_HOME="/home/operator/.config",
               OPENAI_API_KEY="host-provider-key", CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="cli")
    monkeypatch.setattr(os, "environ", env.copy())
    monkeypatch.setattr(acp_client, "_discovered_node_dirs", lambda: ())
    return env


def test_default_scrub_preserves_cli_config_and_host_environment(host_env):
    before = dict(os.environ)
    env = acp_client._launch_env(None)
    assert not set(HOST_VARS) & env.keys()
    assert "OPENAI_API_KEY" not in env
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    for name in ("PATH", "HOME", "XDG_CONFIG_HOME"):
        assert env[name] == host_env[name]
    assert dict(os.environ) == before


@pytest.mark.parametrize("name", HOST_VARS + ("OPENAI_API_KEY",))
def test_exact_passthrough_restores_only_named_var(host_env, name):
    env = acp_client._launch_env(None, env_passthrough=[name])
    assert env[name] == host_env[name]
    assert not (set(HOST_VARS) - {name}) & env.keys()


def test_removals_apply_after_passthrough_and_overlay_still_wins(host_env):
    before = dict(os.environ)
    env = acp_client._launch_env(
        {"PROJECT_MODE": "test", "PROTOAGENT_HOME": "/isolated"},
        env_remove=["PROTOAGENT_", "AGENT_NAME", "XDG_CONFIG_HOME"],
        env_passthrough=[*HOST_VARS, "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"],
    )
    assert env["PROJECT_MODE"] == "test"
    assert env["PROTOAGENT_HOME"] == "/isolated"
    assert "PROTOAGENT_INSTANCE" not in env and "AGENT_NAME" not in env
    assert "XDG_CONFIG_HOME" not in env
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert dict(os.environ) == before


def test_passthrough_is_exact_not_prefix_or_case_insensitive(host_env):
    env = acp_client._launch_env(None, env_passthrough=["PROTOAGENT_", "agent_name", "MISSING"])
    assert not set(HOST_VARS) & env.keys()
    assert "MISSING" not in env


def test_delegate_passthrough_reaches_both_launch_paths(tmp_path, monkeypatch):
    adapter = ADAPTERS["acp"]
    raw = {"name": "coder", "type": "acp", "command": "proto", "workdir": str(tmp_path)}
    default = adapter.parse(raw)
    delegate = adapter.parse({**raw, "env_passthrough": ["AGENT_NAME"]})
    spec = adapter._spec(delegate)
    assert default.env_passthrough == []
    assert spec["env_passthrough"] == ["AGENT_NAME"]
    assert coding_agent._delegate_spec(delegate)["env_passthrough"] == ["AGENT_NAME"]
    assert coding_agent._cache_key(spec) != coding_agent._cache_key(adapter._spec(default))
    assert any(f.key == "env_passthrough" for f in adapter.config_schema())
    monkeypatch.setattr(coding_agent, "_CLIENTS", {})
    monkeypatch.setattr(coding_agent, "_session_id_path", lambda spec: tmp_path / "session.json")
    pooled = coding_agent._client_for(spec)
    _, tapped = coding_agent._tapped_client(coding_agent._delegate_spec(delegate))
    assert pooled.env_passthrough == tapped.env_passthrough == ["AGENT_NAME"]
