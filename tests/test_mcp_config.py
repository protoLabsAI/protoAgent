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
