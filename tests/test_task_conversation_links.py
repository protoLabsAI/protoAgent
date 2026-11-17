"""Related-conversation links on the tasks store (P2b, ADR 0131 "Related
conversations and original Chat").

A task records each original behind it — who asked, who coded, who reviewed — as a
``{ref: "conv:<owner>/<context_id>", kind: "conversation", roles, owner, context_id,
state, reason}`` entry in the same ``links`` JSON, so a later phase can open the right
transcript with its role and owner-agent identity. The link is provenance, never an
authorization input, and (like ``url``) is informational for completion: it never
blocks, drives or gets resolved.

Everything here runs over the REAL store, and the persistence checks reopen a fresh
:class:`TaskStore` on the same db file to prove the entries round-trip through SQLite.
"""

from __future__ import annotations

import pytest

from tasks.store import TaskStore, parse_links


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "issues.db")


@pytest.fixture
def store(db_path):
    return TaskStore(db_path=db_path)


def _conv(links):
    """The conversation entries of a row/links value, keyed by ref."""
    return {e["ref"]: e for e in parse_links(links) if e.get("kind") == "conversation"}


# ── r1: role/owner/context_id persist across a reopen ────────────────────────────


def test_conversation_link_persists_role_owner_context_across_reopen(store, db_path):
    row = store.create(
        "ship the digest",
        links=[{"kind": "conversation", "role": "requester", "owner": "claude-code", "context_id": "chat-1"}],
    )
    entry = _conv(row)["conv:claude-code/chat-1"]
    assert entry == {
        "ref": "conv:claude-code/chat-1",
        "kind": "conversation",
        "roles": ["requester"],
        "owner": "claude-code",
        "context_id": "chat-1",
        "state": "",
        "reason": "",
    }

    # Reopen a fresh store on the same file: the entry survives the SQLite round-trip.
    reopened = TaskStore(db_path=db_path)
    persisted = _conv(reopened.get(row["id"]))["conv:claude-code/chat-1"]
    assert persisted["roles"] == ["requester"]
    assert persisted["owner"] == "claude-code"
    assert persisted["context_id"] == "chat-1"


def test_conversation_links_coexist_with_work_and_url_links(store, db_path):
    row = store.create(
        "mixed",
        links=[
            "bd-c8r5h",
            "https://example.com/notes",
            {"kind": "conversation", "role": "coder", "owner": "codex", "context_id": "chat-9"},
        ],
    )
    reopened = TaskStore(db_path=db_path)
    entries = parse_links(reopened.get(row["id"]))
    assert [(e["ref"], e["kind"]) for e in entries] == [
        ("bd-c8r5h", "card"),
        ("https://example.com/notes", "url"),
        ("conv:codex/chat-9", "conversation"),
    ]
    assert _conv(entries)["conv:codex/chat-9"]["roles"] == ["coder"]


# ── r2: one original, several roles, one entry ───────────────────────────────────


def test_same_original_two_roles_is_one_link_within_one_create(store):
    row = store.create(
        "one original, two roles",
        links=[
            {"kind": "conversation", "role": "requester", "owner": "vera", "context_id": "ctx-7"},
            {"kind": "conversation", "role": "reviewer", "owner": "vera", "context_id": "ctx-7"},
        ],
    )
    convs = _conv(row)
    assert list(convs) == ["conv:vera/ctx-7"]  # one entry, not two
    assert convs["conv:vera/ctx-7"]["roles"] == ["requester", "reviewer"]


def test_same_original_second_role_added_via_update_keeps_both(store, db_path):
    row = store.create(
        "grows a role",
        links=[{"kind": "conversation", "role": "requester", "owner": "vera", "context_id": "ctx-7"}],
    )
    store.update(
        row["id"],
        links=[{"kind": "conversation", "role": "coder", "owner": "vera", "context_id": "ctx-7"}],
    )
    reopened = TaskStore(db_path=db_path)
    convs = _conv(reopened.get(row["id"]))
    assert list(convs) == ["conv:vera/ctx-7"]
    assert convs["conv:vera/ctx-7"]["roles"] == ["requester", "coder"]  # old role kept, new added


def test_duplicate_role_is_not_duplicated(store):
    row = store.create(
        "same role twice",
        links=[
            {"kind": "conversation", "role": "coder", "owner": "codex", "context_id": "ctx-1"},
            {"kind": "conversation", "role": "coder", "owner": "codex", "context_id": "ctx-1"},
        ],
    )
    assert _conv(row)["conv:codex/ctx-1"]["roles"] == ["coder"]


# ── r3: invalid role / missing owner or context_id raises ────────────────────────


def test_unknown_role_raises(store):
    with pytest.raises(ValueError, match="role"):
        store.create("bad", links=[{"kind": "conversation", "role": "owner", "owner": "a", "context_id": "c"}])


def test_missing_owner_raises(store):
    with pytest.raises(ValueError, match="owner"):
        store.create("bad", links=[{"kind": "conversation", "role": "coder", "owner": "", "context_id": "c"}])


def test_missing_context_id_raises(store):
    with pytest.raises(ValueError, match="context_id"):
        store.create("bad", links=[{"kind": "conversation", "role": "coder", "owner": "a", "context_id": "  "}])


def test_unknown_role_on_update_raises_and_leaves_row_unchanged(store):
    row = store.create(
        "keep me",
        links=[{"kind": "conversation", "role": "requester", "owner": "a", "context_id": "c"}],
    )
    with pytest.raises(ValueError):
        store.update(row["id"], links=[{"kind": "conversation", "role": "nope", "owner": "a", "context_id": "c"}])
    # The failed write rolled back: the original role is intact.
    assert _conv(store.get(row["id"]))["conv:a/c"]["roles"] == ["requester"]


# ── r4: owner is part of identity ────────────────────────────────────────────────


def test_same_context_two_owners_stays_two_distinct_links(store, db_path):
    row = store.create(
        "two owners, one context",
        links=[
            {"kind": "conversation", "role": "coder", "owner": "codex", "context_id": "chat-5"},
            {"kind": "conversation", "role": "reviewer", "owner": "vera", "context_id": "chat-5"},
        ],
    )
    reopened = TaskStore(db_path=db_path)
    convs = _conv(reopened.get(row["id"]))
    assert set(convs) == {"conv:codex/chat-5", "conv:vera/chat-5"}  # not collapsed
    assert convs["conv:codex/chat-5"]["roles"] == ["coder"]
    assert convs["conv:vera/chat-5"]["roles"] == ["reviewer"]


# ── r5: informational for completion — never block, close or resolve ─────────────


def test_conversation_only_task_never_auto_closes(store):
    row = store.create(
        "transcript only",
        links=[{"kind": "conversation", "role": "requester", "owner": "a", "context_id": "c"}],
    )
    assert row["status"] == "open"
    # Replacing the links (the update auto-close path) still must not close it.
    updated = store.update(
        row["id"],
        links=[{"kind": "conversation", "role": "reviewer", "owner": "a", "context_id": "c"}],
    )
    assert updated["status"] == "open" and not updated["closed_at"]


def test_conversation_link_does_not_block_work_links_from_closing(store):
    row = store.create(
        "card plus transcript",
        links=[
            "bd-c8r5h",
            {"kind": "conversation", "role": "coder", "owner": "codex", "context_id": "c"},
        ],
    )
    store.resolve_link("bd-c8r5h", "merged (#5146)")
    closed = store.get(row["id"])
    assert closed["status"] == "closed"
    # Only the work link is attributed in the close reason; the conversation is not.
    assert closed["close_reason"] == "bd-c8r5h merged (#5146)"
    assert _conv(closed)["conv:codex/c"]["roles"] == ["coder"]  # kept, untouched


def test_resolve_link_ignores_a_conversation_ref(store):
    row = store.create(
        "transcript only",
        links=[{"kind": "conversation", "role": "requester", "owner": "a", "context_id": "c"}],
    )
    before = store.get(row["id"])
    assert store.resolve_link("conv:a/c", "merged") == []  # matches nothing
    after = store.get(row["id"])
    assert after["status"] == "open"
    assert after["updated_at"] == before["updated_at"]  # untouched
