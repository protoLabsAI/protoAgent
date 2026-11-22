"""Batched inbox recovery turns (ADR 0129 amendment, Fixes #5519).

Several pending inbox items from one source, claimed in one recovery pass, run as ONE
self-A2A turn under a deterministic batch identity recorded on every member's
``inbox_executions`` row — so the agent replies once, and a retry after an uncertain
send still joins the accepted turn instead of running the items twice.

Driven through the real ``InboxStore`` (a temporary database), the real recovery pass
(``server.stores.recover_pending_now_inbox_items``) and the real self-A2A hook
(``server.a2a._fire_activity_from_inbox`` + its batch fire). Only the A2A endpoint is a
fake: an in-process FastAPI app that dedups on ``messageId`` the way the A2A layer does.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

import runtime.state as rs
import server.a2a as a2a
from inbox.store import InboxStore, StormGuard, batch_identity
from server import stores


class _DedupA2A:
    """A fake ``/a2a`` that joins a re-sent ``messageId`` to its existing turn.

    ``mode``: ``ok`` accepts; ``lost`` starts the turn but loses the response (HTTP 502,
    an UNCERTAIN send); ``refuse`` answers a JSON-RPC error; ``failed`` answers a
    terminal failed task state (a dedup echo of a dead task)."""

    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.turns: dict[str, str] = {}
        self.mode = "ok"
        self.on_post = None
        self.app = FastAPI()

        @self.app.post("/a2a")
        async def _a2a(body: dict):
            self.posted.append(body)
            if self.on_post is not None:
                self.on_post(body)
            mid = body["params"]["message"]["messageId"]
            if self.mode == "refuse":
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "refused"}}
            task_id = self.turns.setdefault(mid, f"task-{len(self.turns) + 1}")
            if self.mode == "lost":
                return JSONResponse({"detail": "gateway lost the response"}, status_code=502)
            state = "TASK_STATE_FAILED" if self.mode == "failed" else "TASK_STATE_SUBMITTED"
            return {"result": {"task": {"id": task_id, "status": {"state": state}}}}

    def messages(self) -> list[dict]:
        return [p["params"]["message"] for p in self.posted]


@pytest.fixture(autouse=True)
def _clean_watch_state():
    tables = (a2a._INBOX_TURN_WATCH, a2a._INBOX_EARLY_TERMINALS, a2a._INBOX_RESTORE_OWED, a2a._INBOX_EVICTED_WATCH)
    for t in tables:
        t.clear()
    yield
    for t in tables:
        t.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = InboxStore(str(tmp_path / "inbox.db"))
    fake = _DedupA2A()
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "fastapi_app", fake.app, raising=False)
    monkeypatch.setattr(rs.STATE, "active_port", 9, raising=False)
    monkeypatch.setattr(rs.STATE, "storm_guard", None, raising=False)
    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", a2a._fire_activity_from_inbox, raising=False)
    monkeypatch.setattr(a2a._event_bus, "publish", lambda *_a, **_k: None)
    return store, fake


def _executions(store: InboxStore, ids: list[int]) -> dict[int, sqlite3.Row]:
    db = sqlite3.connect(store.path)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            f"SELECT * FROM inbox_executions WHERE item_id IN ({','.join('?' for _ in ids)})", tuple(ids)
        ).fetchall()
        return {int(r["item_id"]): r for r in rows}
    finally:
        db.close()


def _batch_rows(store: InboxStore) -> list[sqlite3.Row]:
    db = sqlite3.connect(store.path)
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM inbox_batches ORDER BY created_at").fetchall()
    finally:
        db.close()


def _pending(store: InboxStore) -> list[dict]:
    return store.list(priority_floor="later")


async def _recover():
    return await stores.recover_pending_now_inbox_items(limit=8)


@pytest.mark.asyncio
async def test_same_source_items_run_as_one_turn_with_one_shared_identity(env):
    """r1: three same-source now items → ONE SendMessage holding all three texts, each
    labelled with its id and keeping its own late-delivery notice; all three delivered;
    their execution rows share one context, message id and task id."""
    store, fake = env
    late = datetime.now(UTC) - timedelta(hours=3)
    a = store.add("deploy finished", priority="now", source="ci", now=late)
    b = store.add("tests are red", priority="now", source="ci")
    c = store.add("rollback started", priority="now", source="ci")
    ids = [a["id"], b["id"], c["id"]]

    assert await _recover() == {"claimed": 3, "accepted": 3, "failed": 0}

    [msg] = fake.messages()
    text = msg["parts"][0]["text"]
    for item in (a, b, c):
        assert item["text"] in text
        assert f"Inbox item {item['id']}" in text
    assert (
        text.index(f"Inbox item {a['id']}") < text.index(f"Inbox item {b['id']}") < text.index(f"Inbox item {c['id']}")
    )
    # The late member keeps its own staleness line; the fresh members carry none.
    assert text.count("delivered 3h late") == 1
    assert msg["contextId"].startswith("job:")
    assert msg["metadata"] == {
        "origin": "inbox", "inbox_source": "ci", "priority": "now",
        "inbox_ids": ids, "inbox_id": a["id"], "created_at": a["created_at"],
    }

    assert _pending(store) == []
    rows = _executions(store, ids)
    assert {r["context_id"] for r in rows.values()} == {msg["contextId"]}
    assert {r["message_id"] for r in rows.values()} == {msg["messageId"]}
    assert {r["task_id"] for r in rows.values()} == {"task-1"}
    [batch] = _batch_rows(store)
    assert batch["task_id"] == "task-1" and batch["retired_at"] is None

    # Provenance resolves for the shared context, exposing the full membership.
    request = store.request_for_context(msg["contextId"])["request"]
    assert request["item_id"] == a["id"] and request["item_ids"] == ids
    # The identity is the deterministic derivation from the sorted members + generation 0.
    assert (msg["contextId"], msg["messageId"]) == batch_identity(store.store_namespace(), list(reversed(ids)), 0)


@pytest.mark.asyncio
async def test_uncertain_batch_send_retries_under_the_same_identity_and_joins_one_turn(env):
    """r2: the batch's response is lost (the turn DID start) → every member is restored
    with its binding kept; the next pass re-sends the same contextId + messageId, so the
    dedup-joining A2A reports ONE turn, not two."""
    store, fake = env
    items = [store.add(f"sister note {n}", priority="now", source="peer") for n in range(3)]
    ids = [i["id"] for i in items]

    fake.mode = "lost"
    assert await _recover() == {"claimed": 3, "accepted": 0, "failed": 3}
    pending = _pending(store)
    assert [p["id"] for p in pending] == ids
    assert all(p["recovery_claimed_at"] is None for p in pending)
    assert len(_executions(store, ids)) == 3  # the uncertain send keeps the frozen binding

    fake.mode = "ok"
    assert await _recover() == {"claimed": 3, "accepted": 3, "failed": 0}
    first, retry = fake.messages()
    assert (retry["contextId"], retry["messageId"]) == (first["contextId"], first["messageId"])
    assert retry["parts"] == first["parts"]  # the frozen snapshot is re-sent
    assert len(fake.turns) == 1  # one turn, joined — never a second invocation
    assert _pending(store) == []
    assert {r["task_id"] for r in _executions(store, ids).values()} == {"task-1"}


@pytest.mark.asyncio
async def test_different_sources_fire_separately_and_a_group_of_one_is_unchanged(env):
    """r3: two items from A and one from B → two SendMessage calls; the B singleton keeps
    today's per-item wire shape (its own inbox_id, plain text)."""
    store, fake = env
    a1 = store.add("A first", priority="now", source="alpha")
    b1 = store.add("B only", priority="now", source="beta")
    a2 = store.add("A second", priority="now", source="alpha")

    assert await _recover() == {"claimed": 3, "accepted": 3, "failed": 0}
    batch_msg, single_msg = fake.messages()  # first-member claim order
    assert "A first" in batch_msg["parts"][0]["text"] and "A second" in batch_msg["parts"][0]["text"]
    assert "B only" not in batch_msg["parts"][0]["text"]
    assert batch_msg["metadata"]["inbox_ids"] == [a1["id"], a2["id"]]
    assert single_msg["parts"] == [{"text": "B only"}]
    assert single_msg["metadata"] == {
        "origin": "inbox",
        "inbox_id": b1["id"],
        "inbox_source": "beta",
        "priority": "now",
        "created_at": b1["created_at"],
    }
    assert single_msg["contextId"] != batch_msg["contextId"]
    assert _pending(store) == []


@pytest.mark.asyncio
async def test_priority_and_origin_split_groups(env):
    """Grouping is by exact source AND priority AND origin chat: none of these mix."""
    store, fake = env
    store.add("now one", priority="now", source="ci")
    store.add("next one", priority="next", source="ci")
    store.add("from chat", priority="now", source="ci", origin_session="chat-1")

    result = await stores.recover_pending_now_inbox_items(limit=8, priorities=("now", "next"))
    assert result == {"claimed": 3, "accepted": 3, "failed": 0}
    assert len(fake.posted) == 3
    assert all("inbox_ids" not in m["metadata"] for m in fake.messages())


@pytest.mark.asyncio
async def test_refused_batch_send_restores_each_member_under_its_own_claim(env):
    """r4: a refused batch restores every member to pending, each scoped to its OWN
    claim — a member a concurrent owner delivered mid-send stays delivered."""
    store, fake = env
    items = [store.add(f"retry storm {n}", priority="now", source="hook") for n in range(3)]
    ids = [i["id"] for i in items]
    fake.mode = "refuse"
    fake.on_post = lambda _body: store.mark_delivered([ids[1]])  # another owner took it

    assert await _recover() == {"claimed": 3, "accepted": 0, "failed": 3}
    assert len(fake.posted) == 1  # one batched fire, not one per member
    pending = {p["id"]: p for p in _pending(store)}
    assert set(pending) == {ids[0], ids[2]}
    for p in pending.values():
        assert p["recovery_claimed_at"] is None
        assert p["recovery_attempts"] == 1  # an attempted send keeps its charge
        assert "not accepted" in (p["recovery_error"] or "")
    assert store.get(ids[1])["delivered_at"] is not None  # never reopened


@pytest.mark.asyncio
async def test_terminal_failed_echo_retires_every_member_and_the_next_pass_runs_fresh(env):
    """r4: a terminal failed dedup echo retires the WHOLE batch identity at once
    (tombstone kept, generation advanced); the restored members' next pass runs as a
    fresh invocation under a new identity."""
    store, fake = env
    items = [store.add(f"echo {n}", priority="now", source="hook") for n in range(2)]
    ids = [i["id"] for i in items]

    fake.mode = "failed"
    assert await _recover() == {"claimed": 2, "accepted": 0, "failed": 2}
    assert [p["id"] for p in _pending(store)] == ids
    assert _executions(store, ids) == {}  # every member's identity retired
    [dead] = _batch_rows(store)
    assert dead["retired_at"] is not None and dead["generation"] == 0

    fake.mode = "ok"
    assert await _recover() == {"claimed": 2, "accepted": 2, "failed": 0}
    first, fresh = fake.messages()
    assert fresh["contextId"] != first["contextId"] and fresh["messageId"] != first["messageId"]
    assert (fresh["contextId"], fresh["messageId"]) == batch_identity(store.store_namespace(), ids, 1)


@pytest.mark.asyncio
async def test_accepted_batch_whose_turn_later_fails_restores_every_member(env):
    """The task watch tracks every member: a later failed terminal restores all of them
    and retires the batch once."""
    store, fake = env
    items = [store.add(f"page {n}", priority="now", source="pager") for n in range(3)]
    ids = [i["id"] for i in items]
    assert await _recover() == {"claimed": 3, "accepted": 3, "failed": 0}

    a2a._inbox_turn_ended(type("O", (), {"origin": "inbox", "task_id": "task-1", "state": "failed"})())
    for _ in range(200):
        if len(_pending(store)) == 3:
            break
        await asyncio.sleep(0.01)
    assert [p["id"] for p in _pending(store)] == ids
    assert _executions(store, ids) == {}
    assert _batch_rows(store)[0]["retired_at"] is not None


@pytest.mark.asyncio
async def test_storm_guard_counts_one_batched_fire_as_one(env, monkeypatch):
    """A batch of three spends ONE StormGuard fire."""
    store, fake = env
    guard = StormGuard(max_fires=1, window_s=60)
    monkeypatch.setattr(rs.STATE, "storm_guard", guard, raising=False)
    for n in range(3):
        store.add(f"burst {n}", priority="now", source="burst")

    assert await _recover() == {"claimed": 3, "accepted": 3, "failed": 0}
    assert len(fake.posted) == 1
    assert len(guard._fires) == 1


@pytest.mark.asyncio
async def test_busy_bail_refunds_every_batch_member(env):
    store, fake = env
    for n in range(2):
        store.add(f"busy {n}", priority="now", source="ci")

    result = await stores.recover_pending_now_inbox_items(limit=8, is_busy=lambda: True)
    assert result == {"claimed": 2, "accepted": 0, "failed": 0}
    assert fake.posted == []
    for p in _pending(store):
        assert p["recovery_claimed_at"] is None and p["recovery_attempts"] == 0


def test_bind_aborts_when_a_member_claim_changed(tmp_path):
    """Binding revalidates every member's claim in one transaction; a changed claim
    aborts the whole binding — a batch is never half-bound."""
    store = InboxStore(str(tmp_path / "inbox.db"))
    store.add("x", priority="now", source="s")
    store.add("y", priority="now", source="s")
    claimed = store.claim_now_recovery_batch(limit=8)
    stale = [dict(claimed[0]), {**claimed[1], "recovery_claimed_at": "1999-01-01T00:00:00+00:00"}]
    assert store.bind_batch_identity(stale, {"text": "t", "metadata": {}}) is None
    assert _executions(store, [c["id"] for c in claimed]) == {}
    bound = store.bind_batch_identity(claimed, {"text": "t", "metadata": {}})
    assert bound is not None and bound["member_ids"] == sorted(c["id"] for c in claimed)
    # A member that is already bound can never be bound into another batch.
    assert store.bind_batch_identity(claimed, {"text": "t", "metadata": {}}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_fails", [False, True])
async def test_partial_claim_of_an_uncertain_batch_resends_the_full_frozen_snapshot(env, monkeypatch, mark_fails):
    """A pass that claims only SOME members of an unresolved batch never sends a reduced
    batch under a new identity: it re-sends the original full snapshot under the original
    identity, and acceptance completes every frozen member."""
    store, fake = env
    a = store.add("first of pair", priority="now", source="peer")
    store.add("second of pair", priority="now", source="peer")
    fake.mode = "lost"
    assert await _recover() == {"claimed": 2, "accepted": 0, "failed": 2}

    # Only b is claimable on the next pass (a's attempt budget is spent).
    db = sqlite3.connect(store.path)
    db.execute("UPDATE inbox SET recovery_attempts = 99 WHERE id = ?", (a["id"],))
    db.commit()
    db.close()

    mark_delivered = store.mark_delivered

    def mark(ids):
        if mark_fails and ids == [a["id"]]:
            raise OSError("write unavailable")
        return mark_delivered(ids)

    monkeypatch.setattr(store, "mark_delivered", mark)
    fake.mode = "ok"
    assert await _recover() == {"claimed": 1, "accepted": 1, "failed": 0}
    first, retry = fake.messages()
    assert (retry["contextId"], retry["messageId"]) == (first["contextId"], first["messageId"])
    assert "first of pair" in retry["parts"][0]["text"] and "second of pair" in retry["parts"][0]["text"]
    assert len(fake.turns) == 1
    assert _pending(store) == []  # a rode the accepted turn and is completed too
    if mark_fails:
        assert store.get(a["id"])["recovery_accepted_at"]
        assert store.get(a["id"])["delivered_at"] is None
        monkeypatch.setattr(store, "mark_delivered", mark_delivered)
        # Durable completion bypasses the exhausted attempt budget, even while busy.
        assert await stores.recover_pending_now_inbox_items(is_busy=lambda: True) == {
            "claimed": 1, "accepted": 1, "failed": 0,
        }
        assert store.get(a["id"])["delivered_at"]
        assert len(fake.posted) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_delivered_member_only_completes_remaining_batch_with_acceptance(env, accepted):
    store, fake = env
    items = [store.add(f"note {n}", priority="now", source="peer") for n in range(2)]
    ids = [i["id"] for i in items]
    fake.mode = "refuse"
    assert await _recover() == {"claimed": 2, "accepted": 0, "failed": 2}
    binding = store.batch_bindings(ids)[ids[0]]
    # A pull consumer may deliver one item after the refused send releases its claim.
    store.mark_delivered([ids[0]])
    if accepted:
        store.record_batch_acceptance(binding["context_id"], "accepted-task")

    assert await _recover() == {"claimed": 1, "accepted": int(accepted), "failed": int(not accepted)}
    assert len(fake.posted) == 1
    remaining = store.get(ids[1])
    assert bool(remaining["delivered_at"]) == accepted
    if not accepted:
        assert remaining["recovery_claimed_at"] is None
        assert remaining["recovery_attempts"] == 1
        assert "acceptance is unknown" in remaining["recovery_error"]
        assert store.batch_bindings(ids)[ids[1]]["context_id"] == binding["context_id"]
        assert [p["id"] for p in _pending(store)] == [ids[1]]
