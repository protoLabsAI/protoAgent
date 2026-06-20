"""Tests for the MCP client (tools/mcp_tools.py).

No real MCP servers: MultiServerMCPClient is monkeypatched to return canned
tools so we can exercise connection mapping, the loop-safe blocking runner,
namespacing/denylist/collision filtering, and per-server failure isolation.
The real stdio round-trip is covered by the end-to-end check in the PR.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from graph.config import LangGraphConfig
from tools.mcp_tools import _run_blocking, _server_connection, build_mcp_tools


# ── connection mapping ───────────────────────────────────────────────────────


def test_stdio_connection_mapping() -> None:
    conn = _server_connection(
        {"name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "x"], "env": {"A": "1"}}
    )
    # stdio servers inherit the parent env by default, with the per-server
    # override layered on top.
    assert conn["transport"] == "stdio"
    assert conn["command"] == "npx"
    assert conn["args"] == ["-y", "x"]
    assert conn["env"] == {**os.environ, "A": "1"}


def test_stdio_connection_inherit_env_false() -> None:
    # inherit_env: false → only the explicit per-server env (no parent env).
    conn = _server_connection(
        {"name": "fs", "transport": "stdio", "command": "npx", "inherit_env": False, "env": {"A": "1"}}
    )
    assert conn["env"] == {"A": "1"}


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
    """Patch MultiServerMCPClient so each server returns canned tools or raises.

    ``by_server`` maps server name → list[tool] | Exception.
    """

    class FakeClient:
        def __init__(self, connections, tool_name_prefix=False):
            self.name = next(iter(connections))

        async def get_tools(self):
            result = by_server.get(self.name)
            if isinstance(result, Exception):
                raise result
            return result or []

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)


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
                SimpleNamespace(name="current_time"),  # collides with a core tool
            ],
        },
    )
    cfg = _cfg([{"name": "s", "transport": "stdio", "command": "python", "args": ["s.py"]}])
    cfg.mcp_denylist = ["s__drop"]
    _clients, tools, meta = build_mcp_tools(cfg)
    assert [t.name for t in tools] == ["s__keep"]
    assert meta[0]["tool_count"] == 1


def test_disabled_server_not_connected(monkeypatch) -> None:
    # enabled: false → the server is skipped before any connection attempt.
    calls = {"connected": False}

    class TripwireClient:
        def __init__(self, connections, tool_name_prefix=False):
            calls["connected"] = True

        async def get_tools(self):
            return [SimpleNamespace(name="x__t")]

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", TripwireClient)
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


# ── config round-trip ────────────────────────────────────────────────────────


def test_from_yaml_parses_mcp(tmp_path) -> None:
    p = tmp_path / "langgraph-config.yaml"
    p.write_text(
        "mcp:\n"
        "  enabled: true\n"
        "  timeout_seconds: 12\n"
        "  denylist: [x__y]\n"
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
    assert cfg.mcp_servers[0]["name"] == "echo"


def test_config_to_dict_includes_mcp() -> None:
    from graph.config_io import config_to_dict

    d = config_to_dict(LangGraphConfig(mcp_enabled=True))
    assert d["mcp"]["enabled"] is True
    assert "servers" in d["mcp"] and "denylist" in d["mcp"]
    assert d["mcp"]["scope"] == ""  # tier field round-trips


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
