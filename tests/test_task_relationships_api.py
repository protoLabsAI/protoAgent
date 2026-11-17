"""Operator-API surface for a task's request correlation and related conversations (P2d,
ADR 0131 "Conversations in Work").

Everything here runs through the real FastAPI routes with a real :class:`tasks.store.TaskStore`
and a real durable A2A task store on tmp paths, and re-reads persistence through a FRESHLY
reopened ``TaskStore`` on the same db file — so the owner-authorization gate and the decoded
GET fields are proven end to end, not against a stubbed store.

Owner authorization: a related-conversation link is accepted only when its ``owner`` is THIS
agent's own name and its ``context_id`` is one of this agent's own, non-server conversations
(a row in its A2A task store). A forged owner, an unknown context, a server (``job:``/
``background:``/``system:``/``palette-``) context, or another agent's equal context string is
rejected with 403 and nothing is written. The owner/context is provenance to display, never a
read-access grant.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from operator_api.routes import register_operator_routes
from tasks.store import TaskStore, parse_links

OWN_NAME = "work-agent"  # this agent's own name (identity.name)
OWNED_CTX = "chat-own"  # a conversation this agent owns (a row in its A2A store)
SERVER_CTX = "job:run-1"  # a server/execution context — present as a row, but never linkable


def _conv(links):
    """The conversation entries of a row/links value, keyed by ref."""
    return {e["ref"]: e for e in parse_links(links) if e.get("kind") == "conversation"}


def _reopened(task_db: str) -> TaskStore:
    """A fresh store on the same db file — reads only what was durably committed."""
    return TaskStore(db_path=task_db)


@pytest.fixture
async def api(tmp_path, monkeypatch):
    """A FastAPI app over a real TaskStore + a real A2A task store, with this agent's identity
    and A2A engine wired on ``STATE``. The A2A store holds one owned conversation and one
    server-context row."""
    monkeypatch.delenv("TASKS_DB_PATH", raising=False)
    monkeypatch.delenv("BEADS_DB_PATH", raising=False)

    from a2a.server.context import ServerCallContext
    from a2a.types import a2a_pb2

    from a2a_impl.stores import ReasoningCoalescingTaskStore, make_sqlite_engine, resolve_task_owner
    from runtime.state import STATE

    task_db = str(tmp_path / "issues.db")
    task_store = TaskStore(db_path=task_db)

    engine = make_sqlite_engine(str(tmp_path / "a2a-tasks.db"))
    a2a_store = ReasoningCoalescingTaskStore(engine, owner_resolver=resolve_task_owner)
    await a2a_store.initialize()
    ctx = ServerCallContext()
    await a2a_store.save(a2a_pb2.Task(id="t-own", context_id=OWNED_CTX), ctx)
    # A SERVER context that also EXISTS as a row — proves the prefix rule rejects it on identity,
    # not merely because the context is absent.
    await a2a_store.save(a2a_pb2.Task(id="t-job", context_id=SERVER_CTX), ctx)

    monkeypatch.setattr(STATE, "a2a_task_engine", engine)
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(identity_name=OWN_NAME))

    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
        tasks_store=task_store,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(client=client, task_db=task_db, store=task_store)
    await engine.dispose()


# ── accept: own-owner conversation link persists (create + update) ───────────────


async def test_valid_own_owner_conversation_link_persists(api):
    resp = await api.client.post(
        "/api/tasks/issues",
        json={
            "title": "ship the digest",
            "links": [{"kind": "conversation", "role": "requester", "owner": OWN_NAME, "context_id": OWNED_CTX}],
        },
    )
    assert resp.status_code == 200
    issue_id = resp.json()["issue"]["id"]

    # Re-read through a reopened store: the entry survived the SQLite round-trip.
    row = _reopened(api.task_db).get(issue_id)
    entry = _conv(row)[f"conv:{OWN_NAME}/{OWNED_CTX}"]
    assert entry["roles"] == ["requester"]
    assert entry["owner"] == OWN_NAME
    assert entry["context_id"] == OWNED_CTX


async def test_update_accepts_a_valid_own_owner_conversation_link(api):
    created = (await api.client.post("/api/tasks/issues", json={"title": "grows a link"})).json()["issue"]
    resp = await api.client.patch(
        f"/api/tasks/issues/{created['id']}",
        json={"links": [{"kind": "conversation", "role": "reviewer", "owner": OWN_NAME, "context_id": OWNED_CTX}]},
    )
    assert resp.status_code == 200
    row = _reopened(api.task_db).get(created["id"])
    assert _conv(row)[f"conv:{OWN_NAME}/{OWNED_CTX}"]["roles"] == ["reviewer"]


# ── reject: forged owner / unknown context / another owner / server context ──────


@pytest.mark.parametrize(
    "link",
    [
        pytest.param({"owner": "intruder", "context_id": OWNED_CTX}, id="forged-owner"),
        pytest.param({"owner": OWN_NAME, "context_id": "chat-missing"}, id="unknown-context"),
        pytest.param({"owner": "peer-agent", "context_id": OWNED_CTX}, id="another-agents-equal-context"),
        pytest.param({"owner": OWN_NAME, "context_id": SERVER_CTX}, id="server-context"),
    ],
)
async def test_rejected_create_link_is_403_and_writes_nothing(api, link):
    resp = await api.client.post(
        "/api/tasks/issues",
        json={"title": "nope", "links": [{"kind": "conversation", "role": "requester", **link}]},
    )
    assert resp.status_code == 403
    # No task was created — not even a partial row without the link.
    assert _reopened(api.task_db).list() == []


async def test_rejected_update_link_is_403_and_leaves_the_row_unchanged(api):
    created = (await api.client.post("/api/tasks/issues", json={"title": "keep me"})).json()["issue"]
    before = _reopened(api.task_db).get(created["id"])["links"]

    resp = await api.client.patch(
        f"/api/tasks/issues/{created['id']}",
        json={"links": [{"kind": "conversation", "role": "coder", "owner": "intruder", "context_id": OWNED_CTX}]},
    )
    assert resp.status_code == 403
    # Nothing written: the links column is byte-for-byte what it was before the rejected PATCH.
    assert _reopened(api.task_db).get(created["id"])["links"] == before


async def test_rejected_link_does_not_fall_back_on_owner_or_context_for_access(api):
    """The context EXISTS in this agent's store, but a peer-owned link to it is still refused —
    owner/context is provenance, never a key that grants access to that conversation."""
    resp = await api.client.post(
        "/api/tasks/issues",
        json={
            "title": "impersonation",
            "links": [{"kind": "conversation", "role": "related", "owner": "peer-agent", "context_id": OWNED_CTX}],
        },
    )
    assert resp.status_code == 403
    assert "owner" in resp.json()["detail"]


# ── GET rows decode request / requester / rel_version ────────────────────────────


async def test_get_rows_decode_request_and_requester(api):
    api.store.create(
        "correlated",
        request={"kind": "a2a", "owner": "peer", "context_id": "ctx-9", "task_id": "remote-1"},
        requester={"caller": "Josh", "caller_kind": "operator"},
    )
    row = (await api.client.get("/api/tasks/issues")).json()["issues"][0]
    assert row["rel_version"] == 1
    assert row["request"] == {"kind": "a2a", "owner": "peer", "context_id": "ctx-9", "task_id": "remote-1"}
    assert row["requester"] == {"caller": "Josh", "caller_kind": "operator"}
    # The raw storage column is surfaced only as the decoded `request` object.
    assert "request_ref" not in row


async def test_get_rows_without_relationships_decode_to_empty_objects(api):
    api.store.create("plain")
    row = (await api.client.get("/api/tasks/issues")).json()["issues"][0]
    assert row["request"] == {}
    assert row["requester"] == {}
    assert row["rel_version"] == 1  # new creates opt into version 1


# ── request / requester / promotion_key are not writable over the API ────────────


async def test_request_requester_promotion_key_are_ignored_on_create_and_update(api):
    resp = await api.client.post(
        "/api/tasks/issues",
        json={
            "title": "try to forge provenance",
            "request": {"kind": "a2a", "owner": "peer", "context_id": "x"},
            "requester": {"caller": "intruder"},
            "promotion_key": "forged-key",
        },
    )
    assert resp.status_code == 200
    issue_id = resp.json()["issue"]["id"]

    persisted = _reopened(api.task_db).get(issue_id)
    assert persisted["request_ref"] == ""
    assert persisted["requester"] == ""
    assert persisted["promotion_key"] == ""

    # The decoded GET view agrees: the forged values never landed.
    got = next(r for r in (await api.client.get("/api/tasks/issues")).json()["issues"] if r["id"] == issue_id)
    assert got["request"] == {}
    assert got["requester"] == {}

    # Same on update: the title changes, the server-derived columns do not.
    resp2 = await api.client.patch(
        f"/api/tasks/issues/{issue_id}",
        json={
            "title": "renamed",
            "request": {"kind": "a2a"},
            "requester": {"caller": "x"},
            "promotion_key": "forged-key-2",
        },
    )
    assert resp2.status_code == 200
    updated = _reopened(api.task_db).get(issue_id)
    assert updated["title"] == "renamed"
    assert updated["request_ref"] == ""
    assert updated["requester"] == ""
    assert updated["promotion_key"] == ""
