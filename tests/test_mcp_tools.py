"""Tests for the MCP client (tools/mcp_tools.py) and its persistent session pool.

Two layers:

* Fake-backed unit tests — both discovery paths (the default pooled one and the
  stateless ``MultiServerMCPClient`` fallback) are monkeypatched to return
  canned tools, exercising connection mapping, the loop-safe blocking runner,
  namespacing/denylist/collision filtering, and per-server failure isolation.
* Real stdio integration tests — a tiny FastMCP fixture server that counts its
  own boots in a temp file proves the pooled session is REUSED across calls
  (one boot total), reconnects after the subprocess dies, degrades to a
  tool-error string when the pool is closed, and that ``persistent: false``
  restores the one-session-per-call behavior.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from graph.config import LangGraphConfig
from tools.mcp_tools import (
    _run_blocking,
    _server_connection,
    build_mcp_tools,
    close_mcp_clients,
)


# ── connection mapping ───────────────────────────────────────────────────────


def test_stdio_connection_mapping(monkeypatch) -> None:
    monkeypatch.setenv("MCP_TEST_PLAINVAR", "keep")
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret")
    conn = _server_connection(
        {"name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "x"], "env": {"A": "1"}}
    )
    # stdio servers inherit the parent env by default, but credential-looking vars
    # are stripped and the per-server override is layered on top.
    assert conn["transport"] == "stdio"
    assert conn["command"] == "npx"
    assert conn["args"] == ["-y", "x"]
    assert conn["env"]["A"] == "1"                     # per-server override wins
    assert conn["env"]["MCP_TEST_PLAINVAR"] == "keep"  # ordinary var inherited
    assert "MCP_TEST_API_KEY" not in conn["env"]       # secret-named var stripped


def test_stdio_connection_inherit_env_false() -> None:
    # inherit_env: false → only the explicit per-server env (no parent env).
    conn = _server_connection(
        {"name": "fs", "transport": "stdio", "command": "npx", "inherit_env": False, "env": {"A": "1"}}
    )
    assert conn["env"] == {"A": "1"}


def test_stdio_inherit_env_true_passes_full(monkeypatch) -> None:
    # inherit_env: true → the FULL parent env, secrets included (escape hatch).
    monkeypatch.setenv("MCP_TEST_API_KEY", "secret")
    conn = _server_connection(
        {"name": "fs", "transport": "stdio", "command": "npx", "inherit_env": True}
    )
    assert conn["env"]["MCP_TEST_API_KEY"] == "secret"


def test_stdio_default_strips_secret_named_vars(monkeypatch) -> None:
    monkeypatch.setenv("FOO_TOKEN", "t")
    monkeypatch.setenv("FOO_SECRET", "s")
    monkeypatch.setenv("FOO_PASSWORD", "p")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/ssh-agent.sock")  # capability handle → stripped
    monkeypatch.setenv("DATABASE_URL", "postgres://u:pw@h/db")  # DSN w/ creds → stripped
    monkeypatch.setenv("SENTRY_DSN", "https://k@sentry/1")      # DSN → stripped
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gw/v1")      # base URL, not a secret → kept
    monkeypatch.setenv("MCP_TEST_PLAIN", "ok")                  # ordinary var → kept
    conn = _server_connection({"name": "fs", "transport": "stdio", "command": "npx"})
    env = conn["env"]
    for stripped in ("FOO_TOKEN", "FOO_SECRET", "FOO_PASSWORD", "SSH_AUTH_SOCK", "DATABASE_URL", "SENTRY_DSN"):
        assert stripped not in env, f"{stripped} should be stripped"
    assert env.get("OPENAI_BASE_URL") == "https://gw/v1"  # base URLs are deliberately kept
    assert env.get("MCP_TEST_PLAIN") == "ok"


def test_http_connection_mapping_and_alias() -> None:
    for transport in ("streamable_http", "http", "streamable-http"):
        conn = _server_connection({"transport": transport, "url": "https://x/mcp"})
        assert conn == {"transport": "streamable_http", "url": "https://x/mcp"}


def test_connection_missing_required_returns_none() -> None:
    assert _server_connection({"transport": "stdio"}) is None  # no command
    assert _server_connection({"transport": "streamable_http"}) is None  # no url


# ── _run_blocking (both loop contexts) ───────────────────────────────────────


def test_run_blocking_no_running_loop() -> None:
    async def coro():
        return 42

    assert _run_blocking(coro(), timeout=5) == 42


def test_run_blocking_inside_running_loop() -> None:
    # Calling from within a running loop must offload to a thread, not deadlock.
    async def outer():
        async def inner():
            return 7

        return _run_blocking(inner(), timeout=5)

    assert asyncio.run(outer()) == 7


# ── build_mcp_tools ──────────────────────────────────────────────────────────


def _fake_client_factory(monkeypatch, *, by_server: dict):
    """Patch BOTH discovery paths so each server returns canned tools or raises.

    ``by_server`` maps server name → list[tool] | Exception. The default
    (persistent-session) path discovers via ``load_mcp_tools`` with a pooled
    session proxy; the ``persistent: false`` fallback goes through
    ``MultiServerMCPClient.get_tools``. Patching both from the same map keeps
    every filtering/merging test exercising the default pooled path.
    """

    class FakeClient:
        def __init__(self, connections, tool_name_prefix=False):
            self.name = next(iter(connections))

        async def get_tools(self):
            result = by_server.get(self.name)
            if isinstance(result, Exception):
                raise result
            return result or []

    async def fake_load_mcp_tools(session, *, server_name=None, tool_name_prefix=False, **_kw):
        result = by_server.get(server_name)
        if isinstance(result, Exception):
            raise result
        return result or []

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)
    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", fake_load_mcp_tools)


def _cfg(servers):
    return LangGraphConfig(mcp_enabled=True, mcp_servers=servers)


def test_disabled_returns_empty() -> None:
    clients, tools, meta = build_mcp_tools(LangGraphConfig(mcp_enabled=False, mcp_servers=[{"name": "x"}]))
    assert (clients, tools, meta) == ([], [], [])


def test_build_collects_tools_and_meta(monkeypatch) -> None:
    _fake_client_factory(monkeypatch, by_server={"echo": [SimpleNamespace(name="echo__echo")]})
    clients, tools, meta = build_mcp_tools(
        _cfg([{"name": "echo", "transport": "stdio", "command": "python", "args": ["s.py"]}])
    )
    assert [t.name for t in tools] == ["echo__echo"]
    assert meta == [{"name": "echo", "transport": "stdio", "tool_count": 1, "tier": "private"}]
    assert len(clients) == 1


def test_plugin_server_injected_and_activates_mcp(monkeypatch) -> None:
    # A plugin-contributed managed server (register_mcp_server) is connected even
    # when mcp.enabled is off — its presence alone activates MCP discovery.
    _fake_client_factory(monkeypatch, by_server={"g": [SimpleNamespace(name="g__send")]})
    entry = {"name": "g", "transport": "stdio", "command": "python", "args": ["g.py"]}
    cfg = LangGraphConfig(mcp_enabled=False, mcp_servers=[])
    _clients, tools, meta = build_mcp_tools(cfg, plugin_servers=[lambda c: entry])
    assert [t.name for t in tools] == ["g__send"]
    assert meta[0]["name"] == "g"


def test_plugin_server_none_skipped(monkeypatch) -> None:
    # A factory returning None (surface off / not connected) contributes nothing,
    # and with no configured servers MCP stays inert.
    _fake_client_factory(monkeypatch, by_server={})
    cfg = LangGraphConfig(mcp_enabled=False, mcp_servers=[])
    clients, tools, meta = build_mcp_tools(cfg, plugin_servers=[lambda c: None])
    assert (clients, tools, meta) == ([], [], [])


def test_plugin_server_replaces_same_named_config_entry(monkeypatch) -> None:
    # A plugin entry named like a configured server replaces it (managed wins).
    _fake_client_factory(monkeypatch, by_server={"g": [SimpleNamespace(name="g__managed")]})
    managed = {"name": "g", "transport": "stdio", "command": "python", "args": ["managed.py"]}
    cfg = _cfg([{"name": "g", "transport": "stdio", "command": "python", "args": ["user.py"]}])
    _clients, tools, _meta = build_mcp_tools(cfg, plugin_servers=[lambda c: managed])
    assert [t.name for t in tools] == ["g__managed"]


def test_plugin_server_factory_error_isolated(monkeypatch) -> None:
    # A throwing factory is logged + skipped, never fatal; other config servers run.
    _fake_client_factory(monkeypatch, by_server={"echo": [SimpleNamespace(name="echo__echo")]})

    def _boom(c):
        raise RuntimeError("bad factory")

    cfg = _cfg([{"name": "echo", "transport": "stdio", "command": "python", "args": ["s.py"]}])
    _clients, tools, _meta = build_mcp_tools(cfg, plugin_servers=[_boom])
    assert [t.name for t in tools] == ["echo__echo"]


def test_denylist_and_core_collision_filtered(monkeypatch) -> None:
    _fake_client_factory(
        monkeypatch,
        by_server={
            "s": [
                SimpleNamespace(name="s__keep"),
                SimpleNamespace(name="s__drop"),  # denylisted
                # An unprefixed name gets normalized to s__current_time — the
                # namespace itself is the shadowing defense, so it binds.
                SimpleNamespace(name="current_time"),
            ],
        },
    )
    cfg = _cfg([{"name": "s", "transport": "stdio", "command": "python", "args": ["s.py"]}])
    cfg.mcp_denylist = ["s__drop"]
    _clients, tools, meta = build_mcp_tools(cfg)
    assert [t.name for t in tools] == ["s__keep", "s__current_time"]
    assert meta[0]["tool_count"] == 2


def test_single_underscore_adapter_prefix_normalized(monkeypatch) -> None:
    # langchain-mcp-adapters 0.2.x names prefixed tools "<server>_<tool>"
    # (single underscore); protoAgent's documented contract is
    # "<server>__<tool>". Discovery must normalize so bare-name
    # include/exclude entries and subagent allowlists keep matching.
    _fake_client_factory(
        monkeypatch,
        by_server={
            "mythx": [
                SimpleNamespace(name="mythx_roll_dice"),
                SimpleNamespace(name="mythx_attack"),
            ],
        },
    )
    cfg = _cfg(
        [
            {
                "name": "mythx",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "srv"],
                "tools": {"include": ["roll_dice"]},
            }
        ]
    )
    _clients, tools, meta = build_mcp_tools(cfg)
    assert [t.name for t in tools] == ["mythx__roll_dice"]
    assert meta[0]["tool_count"] == 1


def test_disabled_server_not_connected(monkeypatch) -> None:
    # enabled: false → the server is skipped before any connection attempt,
    # on BOTH the pooled (default) and the stateless discovery path.
    calls = {"connected": False}

    class TripwireClient:
        def __init__(self, connections, tool_name_prefix=False):
            calls["connected"] = True

        async def get_tools(self):
            return [SimpleNamespace(name="x__t")]

    async def tripwire_load(session, **_kw):
        calls["connected"] = True
        return []

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", TripwireClient)
    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", tripwire_load)
    _clients, tools, meta = build_mcp_tools(
        _cfg([{"name": "x", "transport": "stdio", "command": "python", "args": ["s.py"], "enabled": False}])
    )
    assert tools == [] and meta == []
    assert calls["connected"] is False


def test_include_allowlist_keeps_only_listed(monkeypatch) -> None:
    _fake_client_factory(
        monkeypatch,
        by_server={
            "s": [SimpleNamespace(name="s__a"), SimpleNamespace(name="s__b"), SimpleNamespace(name="s__c")],
        },
    )
    # include matches the bare tool name (what you'd configure).
    cfg = _cfg(
        [
            {
                "name": "s",
                "transport": "stdio",
                "command": "python",
                "args": ["s.py"],
                "tools": {"include": ["a", "c"]},
            }
        ]
    )
    _clients, tools, meta = build_mcp_tools(cfg)
    assert [t.name for t in tools] == ["s__a", "s__c"]
    assert meta[0]["tool_count"] == 2


def test_exclude_drops_listed(monkeypatch) -> None:
    _fake_client_factory(
        monkeypatch,
        by_server={
            "s": [SimpleNamespace(name="s__keep"), SimpleNamespace(name="s__drop")],
        },
    )
    cfg = _cfg(
        [
            {
                "name": "s",
                "transport": "stdio",
                "command": "python",
                "args": ["s.py"],
                "tools": {"exclude": ["drop"]},
            }
        ]
    )
    _clients, tools, _meta = build_mcp_tools(cfg)
    assert [t.name for t in tools] == ["s__keep"]


def test_include_wins_over_same_server_exclude(monkeypatch) -> None:
    # A name in both include and exclude is kept — explicit inclusion wins.
    _fake_client_factory(
        monkeypatch,
        by_server={
            "s": [SimpleNamespace(name="s__a"), SimpleNamespace(name="s__b")],
        },
    )
    cfg = _cfg(
        [
            {
                "name": "s",
                "transport": "stdio",
                "command": "python",
                "args": ["s.py"],
                "tools": {"include": ["a"], "exclude": ["a"]},
            }
        ]
    )
    _clients, tools, _meta = build_mcp_tools(cfg)
    assert [t.name for t in tools] == ["s__a"]


def test_global_denylist_overrides_include(monkeypatch) -> None:
    # The cross-server denylist is the hard safety net — include cannot revive it.
    _fake_client_factory(
        monkeypatch,
        by_server={
            "s": [SimpleNamespace(name="s__a"), SimpleNamespace(name="s__danger")],
        },
    )
    cfg = _cfg(
        [
            {
                "name": "s",
                "transport": "stdio",
                "command": "python",
                "args": ["s.py"],
                "tools": {"include": ["a", "danger"]},
            }
        ]
    )
    cfg.mcp_denylist = ["s__danger"]
    _clients, tools, _meta = build_mcp_tools(cfg)
    assert [t.name for t in tools] == ["s__a"]


def test_one_bad_server_does_not_break_others(monkeypatch) -> None:
    _fake_client_factory(
        monkeypatch,
        by_server={
            "good": [SimpleNamespace(name="good__t")],
            "bad": RuntimeError("connection refused"),
        },
    )
    cfg = _cfg(
        [
            {"name": "good", "transport": "stdio", "command": "python", "args": ["g.py"]},
            {"name": "bad", "transport": "stdio", "command": "python", "args": ["b.py"]},
        ]
    )
    _clients, tools, meta = build_mcp_tools(cfg)
    assert [t.name for t in tools] == ["good__t"]
    assert [m["name"] for m in meta] == ["good"]


def test_invalid_server_entry_skipped(monkeypatch) -> None:
    _fake_client_factory(monkeypatch, by_server={})
    cfg = _cfg([{"name": "noconn", "transport": "stdio"}])  # no command → invalid
    _clients, tools, meta = build_mcp_tools(cfg)
    assert tools == [] and meta == []


# ── persistent-session path selection ────────────────────────────────────────


def _path_tripwires(monkeypatch, *, tools_by_server=()):
    """Patch both discovery paths with tripwires; returns the hit-counter dict."""
    used = {"stateless": 0, "pooled": 0}
    canned = dict(tools_by_server)

    class FakeClient:
        def __init__(self, connections, tool_name_prefix=False):
            self.name = next(iter(connections))
            used["stateless"] += 1

        async def get_tools(self):
            return canned.get(self.name) or []

    async def fake_load(session, *, server_name=None, **_kw):
        used["pooled"] += 1
        return canned.get(server_name) or []

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)
    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", fake_load)
    return used


def test_persistent_default_discovers_via_pool(monkeypatch) -> None:
    from tools.mcp_session_pool import MCPSessionPool

    used = _path_tripwires(monkeypatch, tools_by_server={"s": [SimpleNamespace(name="s__t")]})
    clients, tools, _meta = build_mcp_tools(
        _cfg([{"name": "s", "transport": "stdio", "command": "python", "args": ["s.py"]}])
    )
    assert used == {"stateless": 0, "pooled": 1}
    assert [t.name for t in tools] == ["s__t"]
    # The pool is the connection handle a rebuild must close.
    assert len(clients) == 1 and isinstance(clients[0], MCPSessionPool)
    close_mcp_clients(clients)


def test_global_persistent_off_uses_stateless_clients(monkeypatch) -> None:
    used = _path_tripwires(monkeypatch, tools_by_server={"s": [SimpleNamespace(name="s__t")]})
    cfg = _cfg([{"name": "s", "transport": "stdio", "command": "python", "args": ["s.py"]}])
    cfg.mcp_persistent_sessions = False
    _clients, tools, _meta = build_mcp_tools(cfg)
    assert used == {"stateless": 1, "pooled": 0}
    assert [t.name for t in tools] == ["s__t"]


def test_per_server_persistent_false_opts_out_alone(monkeypatch) -> None:
    # One server opts out (persistent: false) while its sibling stays pooled.
    used = _path_tripwires(
        monkeypatch,
        tools_by_server={
            "pooled": [SimpleNamespace(name="pooled__a")],
            "legacy": [SimpleNamespace(name="legacy__b")],
        },
    )
    cfg = _cfg(
        [
            {"name": "pooled", "transport": "stdio", "command": "python", "args": ["p.py"]},
            {
                "name": "legacy",
                "transport": "stdio",
                "command": "python",
                "args": ["l.py"],
                "persistent": False,
            },
        ]
    )
    clients, tools, _meta = build_mcp_tools(cfg)
    assert used == {"stateless": 1, "pooled": 1}
    assert {t.name for t in tools} == {"pooled__a", "legacy__b"}
    close_mcp_clients(clients)


def test_close_mcp_clients_tolerates_everything() -> None:
    # Stateless clients (no close()), broken close(), and None-ish input are all fine.
    class Boom:
        def close(self):
            raise RuntimeError("nope")

    close_mcp_clients(None)
    close_mcp_clients([])
    close_mcp_clients([SimpleNamespace(), Boom()])


# ── config round-trip ────────────────────────────────────────────────────────


def test_from_yaml_parses_mcp(tmp_path) -> None:
    p = tmp_path / "langgraph-config.yaml"
    p.write_text(
        "mcp:\n"
        "  enabled: true\n"
        "  timeout_seconds: 12\n"
        "  denylist: [x__y]\n"
        "  persistent_sessions: false\n"
        "  servers:\n"
        "    - name: echo\n"
        "      transport: stdio\n"
        "      command: python\n"
        "      args: ['s.py']\n"
    )
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.mcp_enabled is True
    assert cfg.mcp_timeout_seconds == 12
    assert cfg.mcp_denylist == ["x__y"]
    assert cfg.mcp_persistent_sessions is False  # default True, opt-out parses
    assert cfg.mcp_servers[0]["name"] == "echo"
    assert LangGraphConfig().mcp_persistent_sessions is True  # default ON


def test_config_to_dict_includes_mcp() -> None:
    from graph.config_io import config_to_dict

    d = config_to_dict(LangGraphConfig(mcp_enabled=True))
    assert d["mcp"]["enabled"] is True
    assert "servers" in d["mcp"] and "denylist" in d["mcp"]
    assert d["mcp"]["scope"] == ""  # tier field round-trips
    assert d["mcp"]["persistent_sessions"] is True  # session-pool knob round-trips


# ── Box-commons sharing (ADR 0041) ────────────────────────────────────────────


def test_commons_round_trip(tmp_path) -> None:
    from types import SimpleNamespace

    from tools.mcp_tools import read_mcp_commons, write_mcp_commons

    cfg = SimpleNamespace(commons_path=str(tmp_path))
    assert read_mcp_commons(cfg) == []  # absent → empty
    write_mcp_commons(cfg, [{"name": "s", "transport": "stdio", "command": "x"}])
    assert [s["name"] for s in read_mcp_commons(cfg)] == ["s"]


def test_layered_unions_commons_and_tags_tiers(monkeypatch, tmp_path) -> None:
    from tools.mcp_tools import write_mcp_commons

    _fake_client_factory(
        monkeypatch,
        by_server={"shared": [SimpleNamespace(name="shared__a")], "mine": [SimpleNamespace(name="mine__b")]},
    )
    cfg = LangGraphConfig(
        mcp_enabled=True,
        mcp_scope="layered",
        commons_path=str(tmp_path),
        mcp_servers=[{"name": "mine", "transport": "stdio", "command": "python", "args": ["m.py"]}],
    )
    write_mcp_commons(cfg, [{"name": "shared", "transport": "stdio", "command": "python", "args": ["s.py"]}])
    _clients, tools, meta = build_mcp_tools(cfg)
    by_name = {m["name"]: m for m in meta}
    assert by_name["shared"]["tier"] == "commons"
    assert by_name["mine"]["tier"] == "private"
    assert {t.name for t in tools} == {"shared__a", "mine__b"}


def test_layered_commons_activates_mcp_without_enabled(monkeypatch, tmp_path) -> None:
    # Opting into the commons (layered) with a shared server activates MCP even when
    # mcp.enabled is off and there are no private servers.
    from tools.mcp_tools import write_mcp_commons

    _fake_client_factory(monkeypatch, by_server={"shared": [SimpleNamespace(name="shared__a")]})
    cfg = LangGraphConfig(mcp_enabled=False, mcp_scope="layered", commons_path=str(tmp_path), mcp_servers=[])
    write_mcp_commons(cfg, [{"name": "shared", "transport": "stdio", "command": "python", "args": ["s.py"]}])
    _clients, tools, meta = build_mcp_tools(cfg)
    assert [t.name for t in tools] == ["shared__a"]
    assert meta[0]["tier"] == "commons"


def test_scoped_ignores_commons(monkeypatch, tmp_path) -> None:
    from tools.mcp_tools import write_mcp_commons

    _fake_client_factory(monkeypatch, by_server={"shared": [SimpleNamespace(name="shared__a")]})
    cfg = LangGraphConfig(mcp_enabled=False, mcp_scope="scoped", commons_path=str(tmp_path), mcp_servers=[])
    write_mcp_commons(cfg, [{"name": "shared", "transport": "stdio", "command": "python", "args": ["s.py"]}])
    # scoped + no private servers → the commons is not read, MCP stays inert.
    assert build_mcp_tools(cfg) == ([], [], [])


def test_private_shadows_commons_by_name(monkeypatch, tmp_path) -> None:
    from tools.mcp_tools import write_mcp_commons

    _fake_client_factory(monkeypatch, by_server={"dup": [SimpleNamespace(name="dup__x")]})
    cfg = LangGraphConfig(
        mcp_enabled=True,
        mcp_scope="layered",
        commons_path=str(tmp_path),
        mcp_servers=[{"name": "dup", "transport": "stdio", "command": "private", "args": []}],
    )
    write_mcp_commons(cfg, [{"name": "dup", "transport": "stdio", "command": "commons", "args": []}])
    _clients, _tools, meta = build_mcp_tools(cfg)
    assert len(meta) == 1 and meta[0]["tier"] == "private"  # private wins by name


# ── real stdio round-trips (persistent session pool) ─────────────────────────
#
# A tiny FastMCP server that appends one line to a boot file every time it
# starts — the spawn-count oracle. `ping` proves the link; `die` kills the
# server process MID-CALL to exercise the pool's reconnect path.

_FIXTURE_SERVER = '''
import os
import sys

with open(sys.argv[1], "a") as f:
    f.write("boot\\n")

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fix")


@mcp.tool()
def ping(text: str) -> str:
    """Round-trip a string."""
    return "pong:" + text


@mcp.tool()
def die() -> str:
    """Kill the server process mid-call."""
    os._exit(1)


mcp.run(transport="stdio")
'''


def _fixture_server_cfg(tmp_path, **server_extra):
    script = tmp_path / "boot_counter_server.py"
    script.write_text(_FIXTURE_SERVER)
    boot_file = tmp_path / "boots.txt"
    server = {
        "name": "fix",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(script), str(boot_file)],
        **server_extra,
    }
    return _cfg([server]), boot_file


def _boots(boot_file) -> int:
    return boot_file.read_text().count("boot") if boot_file.exists() else 0


def test_persistent_session_reused_across_calls(tmp_path) -> None:
    cfg, boot_file = _fixture_server_cfg(tmp_path)
    clients, tools, meta = build_mcp_tools(cfg)
    try:
        assert {t.name for t in tools} == {"fix__ping", "fix__die"}
        assert meta == [{"name": "fix", "transport": "stdio", "tool_count": 2, "tier": "private"}]
        ping = next(t for t in tools if t.name == "fix__ping")
        # Two invocations from two DIFFERENT event loops — the pool bridges every
        # call onto its own loop, so callers stay loop-agnostic like before.
        assert "pong:a" in str(asyncio.run(ping.ainvoke({"text": "a"})))
        assert "pong:b" in str(asyncio.run(ping.ainvoke({"text": "b"})))
        # ONE server boot covered discovery + both calls — the session is reused
        # (the stateless path would have booted three times).
        assert _boots(boot_file) == 1
    finally:
        close_mcp_clients(clients)


def test_disabled_persistence_opens_session_per_call(tmp_path) -> None:
    # persistent: false → the pre-pool behavior: a fresh session (= subprocess
    # for stdio) for discovery and for EVERY call.
    cfg, boot_file = _fixture_server_cfg(tmp_path, persistent=False)
    clients, tools, _meta = build_mcp_tools(cfg)
    try:
        ping = next(t for t in tools if t.name == "fix__ping")
        assert "pong:a" in str(asyncio.run(ping.ainvoke({"text": "a"})))
        assert "pong:b" in str(asyncio.run(ping.ainvoke({"text": "b"})))
        assert _boots(boot_file) == 3  # discovery + one per call
    finally:
        close_mcp_clients(clients)


def test_reconnect_after_server_death(tmp_path) -> None:
    cfg, boot_file = _fixture_server_cfg(tmp_path)
    clients, tools, _meta = build_mcp_tools(cfg)
    try:
        ping = next(t for t in tools if t.name == "fix__ping")
        die = next(t for t in tools if t.name == "fix__die")

        assert "pong:a" in str(asyncio.run(ping.ainvoke({"text": "a"})))
        assert _boots(boot_file) == 1

        # `die` kills the server mid-call. The pool reconnects ONCE and retries;
        # the retry kills the fresh process too, so the call degrades to the
        # recoverable tool-error string (handle_tool_error) — never a dead turn.
        result = asyncio.run(die.ainvoke({}))
        assert "Tool error" in str(result)
        assert _boots(boot_file) == 2  # the original + exactly one reconnect

        # The NEXT call finds no live session and transparently reconnects.
        assert "pong:b" in str(asyncio.run(ping.ainvoke({"text": "b"})))
        assert _boots(boot_file) == 3
    finally:
        close_mcp_clients(clients)


def test_include_filter_applies_to_pooled_server(tmp_path) -> None:
    # The include allowlist (bare tool names) keeps working over the pooled
    # discovery path, including the <server>__<tool> prefix normalization.
    cfg, _boot_file = _fixture_server_cfg(tmp_path, tools={"include": ["ping"]})
    clients, tools, meta = build_mcp_tools(cfg)
    try:
        assert [t.name for t in tools] == ["fix__ping"]
        assert meta[0]["tool_count"] == 1
        assert "pong:x" in str(asyncio.run(tools[0].ainvoke({"text": "x"})))
    finally:
        close_mcp_clients(clients)


def test_closed_pool_degrades_to_tool_error(tmp_path) -> None:
    # After a config rebuild closes the pool (close_mcp_clients), a stale tool
    # still bound to it returns a recoverable tool-error string — no hang, no
    # raised exception killing the turn.
    cfg, _boot_file = _fixture_server_cfg(tmp_path)
    clients, tools, _meta = build_mcp_tools(cfg)
    ping = next(t for t in tools if t.name == "fix__ping")
    assert "pong:a" in str(asyncio.run(ping.ainvoke({"text": "a"})))
    close_mcp_clients(clients)
    close_mcp_clients(clients)  # idempotent
    result = asyncio.run(ping.ainvoke({"text": "b"}))
    assert "Tool error" in str(result)
