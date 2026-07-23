"""Unit tests for graph.mcp_config — the shared MCP entry normalizer + bundle-seed
template resolver (ADR 0083 D5, #2011). The normalizer used to live in
operator_api/mcp_routes; these lock its behavior at the new graph-level home so the
console routes and the workspace seeder stay in agreement."""

from __future__ import annotations

import pytest

from graph.mcp_config import (
    clean_mcp_entry,
    entries_from_blob,
    normalize_named,
    resolve_bundle_mcp_item,
)


def test_clean_entry_stdio_requires_command_and_splits_args():
    with pytest.raises(ValueError):
        clean_mcp_entry({"name": "x", "transport": "stdio"})
    assert clean_mcp_entry({"name": "x", "transport": "stdio", "command": "npx", "args": "a b"}) == {
        "name": "x",
        "transport": "stdio",
        "command": "npx",
        "args": ["a", "b"],
    }


def test_clean_entry_http_requires_url_and_keeps_headers():
    with pytest.raises(ValueError):
        clean_mcp_entry({"name": "x", "transport": "http"})
    e = clean_mcp_entry({"name": "x", "transport": "http", "url": "https://h/", "headers": {"A": "b"}})
    assert e["url"] == "https://h/"
    assert e["headers"] == {"A": "b"}


def test_clean_entry_rejects_bad_transport_and_empty_name():
    with pytest.raises(ValueError):
        clean_mcp_entry({"name": "x", "transport": "carrier-pigeon", "url": "u"})
    with pytest.raises(ValueError):
        clean_mcp_entry({"name": "", "transport": "stdio", "command": "c"})


def test_normalize_named_infers_transport():
    assert normalize_named("a", {"command": "npx"})["transport"] == "stdio"
    assert normalize_named("a", {"url": "https://h/"})["transport"] == "http"
    assert normalize_named("a", {"type": "streamable-http", "url": "u"})["transport"] == "streamable_http"


def test_entries_from_blob_shapes():
    assert [e["name"] for e in entries_from_blob({"mcpServers": {"a": {"command": "x"}}})] == ["a"]
    servers = {"servers": [{"name": "b", "transport": "stdio", "command": "x"}]}
    assert [e["name"] for e in entries_from_blob(servers)] == ["b"]
    with pytest.raises(ValueError):
        entries_from_blob([])  # not a dict
    with pytest.raises(ValueError):
        entries_from_blob({"nope": 1})  # no server shape found


def test_resolve_bundle_item_fills_from_env_then_default():
    item = {
        "template": {
            "name": "gh",
            "transport": "http",
            "url": "https://h/",
            "headers": {"Authorization": "Bearer ${token}"},
        },
        "inputs": [{"key": "token", "env": "TK", "required": True}],
    }
    entry, unresolved = resolve_bundle_mcp_item(item, {"TK": "s3cr3t"})
    assert unresolved == []
    assert entry["headers"]["Authorization"] == "Bearer s3cr3t"

    from_default, unresolved2 = resolve_bundle_mcp_item(
        {
            "template": {"name": "fs", "transport": "stdio", "command": "npx", "args": ["${p}"]},
            "inputs": [{"key": "p", "default": "/w", "required": True}],
        },
        {},  # env empty → falls back to the default
    )
    assert unresolved2 == []
    assert from_default["args"] == ["/w"]


def test_resolve_bundle_item_unresolved_required_blanks_placeholder():
    item = {
        "template": {
            "name": "gh",
            "transport": "http",
            "url": "https://h/",
            "headers": {"Authorization": "Bearer ${token}"},
        },
        "inputs": [{"key": "token", "env": "MISSING", "required": True}],
    }
    entry, unresolved = resolve_bundle_mcp_item(item, {})
    assert unresolved == ["token"]
    assert entry["headers"]["Authorization"] == "Bearer "  # placeholder → "", not left literal


def test_resolve_bundle_item_bare_entry_no_inputs():
    entry, unresolved = resolve_bundle_mcp_item(
        {"name": "plain", "transport": "stdio", "command": "npx"}, {}
    )
    assert unresolved == []
    assert entry["name"] == "plain"
    assert entry["command"] == "npx"


# ── operator-supplied values take priority over env (#2041) ───────────────────
def _gh_item():
    return {
        "template": {
            "name": "gh",
            "transport": "http",
            "url": "https://h/",
            "headers": {"Authorization": "Bearer ${token}"},
        },
        "inputs": [{"key": "token", "env": "TK", "required": True}],
    }


def test_resolve_bundle_item_operator_values_beat_env():
    """An operator-supplied value wins over the input's declared env var."""
    entry, unresolved = resolve_bundle_mcp_item(_gh_item(), {"TK": "from_env"}, {"token": "from_op"})
    assert unresolved == []
    assert entry["headers"]["Authorization"] == "Bearer from_op"


def test_resolve_bundle_item_operator_value_resolves_required_without_env():
    """A required input with no env value or default is resolved purely by the operator value —
    so the entry seeds ENABLED instead of the env-only → unresolved (disabled) fallback."""
    item = {
        "template": {
            "name": "gh",
            "transport": "http",
            "url": "https://h/",
            "headers": {"Authorization": "Bearer ${token}"},
        },
        "inputs": [{"key": "token", "env": "MISSING", "required": True}],
    }
    entry, unresolved = resolve_bundle_mcp_item(item, {}, {"token": "op-token"})
    assert unresolved == []
    assert entry["headers"]["Authorization"] == "Bearer op-token"


def test_resolve_bundle_item_blank_operator_value_falls_through_to_env():
    """A blank/absent operator value doesn't shadow the env fallback."""
    entry, unresolved = resolve_bundle_mcp_item(_gh_item(), {"TK": "envval"}, {"token": ""})
    assert unresolved == []
    assert entry["headers"]["Authorization"] == "Bearer envval"


def test_resolve_bundle_item_no_values_is_env_only_fallback():
    """Omitting `values` (the default) is the pre-existing env-only behavior: a required input
    with no env value stays unresolved so the caller disables it."""
    entry, unresolved = resolve_bundle_mcp_item(_gh_item(), {})
    assert unresolved == ["token"]
    assert entry["headers"]["Authorization"] == "Bearer "


# ── "{server}:{key}"-namespaced values scope one server in a bundle (#2128) ───
def _token_item(name: str):
    """A server item whose sole input key is ``token`` — two of these in a bundle
    collide on the bare key, which is exactly what namespacing disambiguates."""
    return {
        "template": {
            "name": name,
            "transport": "http",
            "url": "https://h/",
            "headers": {"Authorization": "Bearer ${token}"},
        },
        "inputs": [{"key": "token", "env": "TK", "required": True}],
    }


def test_resolve_bundle_item_namespaced_values_scope_per_server():
    """Two servers sharing an input key each get their own `{name}:{key}` value —
    a namespaced match on one server never bleeds into the other."""
    values = {"gh:token": "ghp_x", "bb:token": "bbp_y"}
    gh, gh_unresolved = resolve_bundle_mcp_item(_token_item("gh"), {}, values)
    bb, bb_unresolved = resolve_bundle_mcp_item(_token_item("bb"), {}, values)
    assert gh_unresolved == [] and bb_unresolved == []
    assert gh["headers"]["Authorization"] == "Bearer ghp_x"
    assert bb["headers"]["Authorization"] == "Bearer bbp_y"


def test_resolve_bundle_item_bare_key_unchanged_for_single_server():
    """A bare-key caller resolves exactly as before — the namespaced lookup misses
    and falls through to `values[key]`."""
    entry, unresolved = resolve_bundle_mcp_item(_token_item("gh"), {}, {"token": "s3cr3t"})
    assert unresolved == []
    assert entry["headers"]["Authorization"] == "Bearer s3cr3t"


def test_resolve_bundle_item_namespaced_beats_bare_without_shadowing_other_server():
    """`gh:token` wins over the bare `token` for gh only; bb still falls back to
    the bare key."""
    values = {"gh:token": "ghp_x", "token": "shared"}
    gh, _ = resolve_bundle_mcp_item(_token_item("gh"), {}, values)
    bb, _ = resolve_bundle_mcp_item(_token_item("bb"), {}, values)
    assert gh["headers"]["Authorization"] == "Bearer ghp_x"
    assert bb["headers"]["Authorization"] == "Bearer shared"


def test_resolve_bundle_item_blank_namespaced_value_falls_through():
    """A blank namespaced value doesn't shadow the bare key or the env fallback —
    same falsy fall-through the bare operator value already has."""
    entry, unresolved = resolve_bundle_mcp_item(
        _token_item("gh"), {}, {"gh:token": "", "token": "bare"}
    )
    assert unresolved == []
    assert entry["headers"]["Authorization"] == "Bearer bare"

    from_env, unresolved2 = resolve_bundle_mcp_item(
        _token_item("gh"), {"TK": "envval"}, {"gh:token": ""}
    )
    assert unresolved2 == []
    assert from_env["headers"]["Authorization"] == "Bearer envval"
