"""Integrity of the curated MCP quick-add catalog (`config/mcp-catalog.json`).

A catalog entry only ever executes when a human clicks it in Settings ▸ MCP ▸ Browse,
so a broken one fails in front of a user and nowhere else. That is not hypothetical:
this catalog shipped `@modelcontextprotocol/server-sequentialthinking` for weeks after
upstream renamed the package to `server-sequential-thinking`, i.e. a 404 behind a
button, found only by auditing it by hand.

Whether a package still EXISTS needs the network and can't be a unit test — that stays
a periodic manual audit (the `_comment` records when it was last done). What is checked
here is everything that can go stale *offline*, in particular the failure mode with no
other backstop: a `${placeholder}` with no matching input, which renders an entry
permanently unfillable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

CATALOG = Path(__file__).resolve().parents[1] / "config" / "mcp-catalog.json"
_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


def _servers() -> list[dict]:
    return json.loads(CATALOG.read_text())["servers"]


def _placeholders(value) -> set[str]:
    """Every ``${key}`` anywhere in a template — they hide in nested args lists,
    header dicts and env dicts, not just top-level strings."""
    if isinstance(value, str):
        return set(_PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        return set().union(*(_placeholders(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_placeholders(v) for v in value)) if value else set()
    return set()


@pytest.mark.parametrize("server", _servers(), ids=lambda s: s["id"])
def test_every_placeholder_has_an_input(server: dict) -> None:
    """The failure with no other backstop: a template referencing `${token}` with no
    matching input can never be completed, so the entry is dead on arrival."""
    declared = {i["key"] for i in server.get("inputs", [])}
    used = _placeholders(server["template"])
    assert used <= declared, (
        f"{server['id']}: template uses {sorted(used - declared)} with no matching "
        f"`inputs` entry — the operator would have no field to fill it in."
    )


@pytest.mark.parametrize("server", _servers(), ids=lambda s: s["id"])
def test_every_input_is_actually_used(server: dict) -> None:
    """The mirror image: an input the template never substitutes is a field that asks
    the operator for something and then silently discards it."""
    declared = {i["key"] for i in server.get("inputs", [])}
    used = _placeholders(server["template"])
    assert declared <= used, f"{server['id']}: inputs {sorted(declared - used)} are never substituted"


@pytest.mark.parametrize("server", _servers(), ids=lambda s: s["id"])
def test_transport_carries_the_fields_it_needs(server: dict) -> None:
    """`_server_connection` returns None — the server is silently skipped — when a
    stdio entry has no `command` or a remote one has no `url`."""
    template = server["template"]
    transport = template.get("transport", "stdio")
    if transport == "stdio":
        assert template.get("command"), f"{server['id']}: stdio entry without a command"
    else:
        assert template.get("url"), f"{server['id']}: {transport} entry without a url"


@pytest.mark.parametrize("server", _servers(), ids=lambda s: s["id"])
def test_entry_is_presentable(server: dict) -> None:
    """The picker renders these; a blank card is worse than no card."""
    for key in ("id", "name", "category", "tagline", "docs"):
        assert server.get(key), f"{server['id']}: missing {key}"
    assert server["docs"].startswith("https://"), f"{server['id']}: docs must be an https URL"


def test_ids_and_server_names_are_unique() -> None:
    """A duplicate name would collide in `mcp.servers` on add; a duplicate id breaks
    the installed-flag lookup, which is keyed by it."""
    servers = _servers()
    ids = [s["id"] for s in servers]
    names = [s["template"]["name"] for s in servers]
    assert len(set(ids)) == len(ids), f"duplicate catalog ids: {ids}"
    assert len(set(names)) == len(names), f"duplicate server names: {names}"


def test_the_third_party_filesystem_server_is_not_recommended() -> None:
    """A deliberate omission, pinned so a well-meaning re-add has to read why.

    protoAgent ships fenced filesystem tools (ADR 0007, `filesystem.projects`) that
    cover the same ground with a directory allowlist. The third-party server's
    `search_files` walks its root with no default exclusions (it does not skip
    `node_modules` or `.git`) and no depth or result cap — one call over a 2.75M-entry
    tree measured 222.7s and wedged a live agent turn (#2344).
    """
    blob = json.dumps(json.loads(CATALOG.read_text())["servers"])
    assert "server-filesystem" not in blob
