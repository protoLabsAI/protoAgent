"""The parked-sessions index and session summary read every session in ONE scan (#3972).

``_parked_sessions`` used to confirm each candidate with ``_newest_turn_summary`` — two
queries per candidate — and the SDK's ``tasks`` table has no ``context_id`` index, so each
was a full table scan: up to 1,600 scans for ``?limit=200&parked=true``. These pin the
statement count (independent of how many sessions match) and pin the results to the old
per-session path's on a seeded store with thousands of rows, where creation order and
``last_updated`` order disagree (#3963's re-parked pause)."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

PARKED = ("TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED", "input-required", "auth-required")
OTHER = ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_WORKING", "completed", None)
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _seed(tmp_path, rows):
    from a2a.server.tasks.database_task_store import Base, TaskModel
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/tasks-3972.db")

    async def _go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                TaskModel.__table__.insert(),
                [
                    {
                        "id": r["id"],
                        "context_id": r["context_id"],
                        "kind": "task",
                        "status": {"state": r["state"]} if r["state"] else {},
                        "artifacts": [],
                        "history": [],
                        "last_updated": r["at"],
                    }
                    for r in rows
                ],
            )

    asyncio.run(_go())
    return engine


def _random_store(seed: int, sessions: int, turns_max: int) -> list[dict]:
    """``sessions`` contexts of 1..``turns_max`` turns, inserted in creation order but
    stamped with jittered ``last_updated`` — so a superseded turn can be stamped AFTER the
    newer one, the way a re-parked pause leaves it (#3963)."""
    rng = random.Random(seed)
    rows: list[dict] = []
    minute = 0
    for s in range(sessions):
        prefix = "chat-" if rng.random() < 0.9 else "activity:"
        ctx = f"{prefix}{s:05d}"
        for t in range(rng.randint(1, turns_max)):
            minute += 1
            state = rng.choice(PARKED) if rng.random() < 0.35 else rng.choice(OTHER)
            rows.append(
                {
                    "id": f"{ctx}-t{t}",
                    "context_id": ctx,
                    "state": state,
                    "at": T0 + timedelta(minutes=minute + rng.randint(-3, 3)),
                }
            )
    rng.shuffle(rows)  # interleave contexts; rowid stays each row's creation order
    return rows


async def _old_newest_turn_summary(conn, session_id):
    """The pre-#3972 per-session path, verbatim — the reference the one-scan read must match."""
    from a2a.server.tasks.database_task_store import TaskModel
    from sqlalchemy import func, select

    from a2a_impl.stores import task_newest_first

    agg = (
        await conn.execute(
            select(func.count(TaskModel.id), func.max(TaskModel.last_updated)).where(
                TaskModel.context_id == session_id
            )
        )
    ).first()
    count = int(agg[0] or 0) if agg else 0
    last = agg[1] if agg else None
    newest = None
    if count:
        newest = (
            await conn.execute(
                select(TaskModel.status)
                .where(TaskModel.context_id == session_id)
                .order_by(*task_newest_first(TaskModel, conn.dialect.name))
                .limit(1)
            )
        ).first()
    status = newest[0] if newest else None
    state = ((status or {}).get("state") or None) if isinstance(status, dict) else None
    return count, last, state


async def _old_parked_sessions(engine, limit):
    """The pre-#3972 ``_parked_sessions`` (candidates, then 2 queries per candidate)."""
    import operator_api.chat_routes as cr
    from a2a.server.tasks.database_task_store import TaskModel
    from sqlalchemy import exists, func, select

    state = TaskModel.status["state"].as_string()
    async with engine.begin() as conn:
        tombstones = await cr._ensure_chat_tombstones(conn)
        candidates = (
            await conn.execute(
                select(TaskModel.context_id, func.max(TaskModel.last_updated).label("parked_at"))
                .where(TaskModel.context_id.like("chat-%"))
                .where(state.in_(cr._PARKED_STORED_STATES))
                .where(~exists(select(1).where(tombstones.c.context_id == TaskModel.context_id)))
                .group_by(TaskModel.context_id)
                .order_by(func.max(TaskModel.last_updated).desc().nulls_last(), TaskModel.context_id.desc())
                .limit(limit * 4)
            )
        ).fetchall()
        out = []
        for row in candidates:
            count, last, last_state = await _old_newest_turn_summary(conn, row.context_id)
            if not last_state or not cr._PAUSED_TASK_STATE.search(last_state):
                continue
            out.append(
                {
                    "session_id": row.context_id,
                    "last_updated": last.isoformat() if last else None,
                    "turn_count": count,
                    "last_state": last_state,
                }
            )
            if len(out) >= limit:
                break
    return out


class _TaskReads:
    """Counts the statements that read the SDK ``tasks`` table on ``engine`` while open."""

    def __init__(self, engine):
        self.engine = engine.sync_engine
        self.statements: list[str] = []

    def _count(self, _conn, _cursor, statement, *_a):
        text = " ".join(statement.split()).lower()
        if text.startswith(("select", "with")) and " tasks" in text:
            self.statements.append(text)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._count)
        return self

    def __exit__(self, *_exc):
        event.remove(self.engine, "before_cursor_execute", self._count)


@pytest.fixture
def store(tmp_path):
    rows = _random_store(seed=3972, sessions=1200, turns_max=6)
    assert len(rows) > 3000
    engine = _seed(tmp_path, rows)
    yield engine, rows
    asyncio.run(engine.dispose())


def test_parked_index_reads_the_store_once_however_many_sessions_match(store):
    import operator_api.chat_routes as cr

    engine, _rows = store
    with _TaskReads(engine) as reads:
        got = asyncio.run(cr._parked_sessions(engine, 200))
    # Hundreds of candidates — the old path ran 1 + 2 per candidate (≈ 800 scans here).
    assert len(got) >= 100
    assert len(reads.statements) == 1, f"{len(reads.statements)} task-store reads"


@pytest.mark.parametrize("limit", [1, 7, 20, 50, 200])
def test_parked_index_matches_the_per_session_path(store, limit):
    import operator_api.chat_routes as cr

    engine, _rows = store
    new = asyncio.run(cr._parked_sessions(engine, limit))
    old = asyncio.run(_old_parked_sessions(engine, limit))
    assert new == old
    assert len(new) == min(limit, len(old)) and new  # non-trivial comparison


def test_parked_index_follows_creation_order_not_last_updated(tmp_path):
    """A pause re-parked on a newer task while the superseded one completed AFTER it: the
    context is parked (#3963) — and one whose parked row a later-CREATED turn ran past is
    not, though that row was stamped last."""
    import operator_api.chat_routes as cr

    rows = [
        {"id": "a1", "context_id": "chat-reparked", "state": "TASK_STATE_INPUT_REQUIRED", "at": T0},
        {"id": "a2", "context_id": "chat-reparked", "state": "TASK_STATE_INPUT_REQUIRED", "at": T0 + timedelta(seconds=1)},
        {"id": "b1", "context_id": "chat-ran-past", "state": "TASK_STATE_INPUT_REQUIRED", "at": T0 + timedelta(minutes=9)},
        {"id": "b2", "context_id": "chat-ran-past", "state": "TASK_STATE_COMPLETED", "at": T0 + timedelta(minutes=1)},
    ]
    rows[0]["state"] = "TASK_STATE_COMPLETED"
    rows[0]["at"] = T0 + timedelta(minutes=5)  # superseded, stamped after the re-park
    engine = _seed(tmp_path, rows)
    try:
        got = asyncio.run(cr._parked_sessions(engine, 20))
        assert got == asyncio.run(_old_parked_sessions(engine, 20))
        assert [(r["session_id"], r["turn_count"], r["last_state"]) for r in got] == [
            ("chat-reparked", 2, "TASK_STATE_INPUT_REQUIRED")
        ]
    finally:
        asyncio.run(engine.dispose())


def test_session_summary_reads_the_store_once_and_matches(store, monkeypatch):
    import operator_api.chat_routes as cr
    import runtime.state as rs

    engine, rows = store
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", engine, raising=False)
    contexts = sorted({r["context_id"] for r in rows})[:: 40]

    async def _old(sid):
        async with engine.begin() as conn:
            return await _old_newest_turn_summary(conn, sid)

    for sid in contexts:
        with _TaskReads(engine) as reads:
            body = asyncio.run(cr.session_summary(sid))
        assert len(reads.statements) == 1, f"{sid}: {len(reads.statements)} task-store reads"
        count, last, state = asyncio.run(_old(sid))
        assert (body["turn_count"], body["last_updated"], body["last_state"]) == (
            count,
            last.isoformat() if last else None,
            state,
        )
    assert asyncio.run(cr.session_summary("chat-never-seen")) is None


def test_non_sqlite_stores_rank_by_last_change():
    """No ``rowid`` off SQLite: the window falls back to ``task_newest_first``'s
    last-change order (#3965) rather than failing the read."""
    from a2a.server.tasks.database_task_store import TaskModel
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql, sqlite

    import operator_api.chat_routes as cr

    def _sql(dialect_name, dialect):
        ranked = cr._newest_turn_ranked(TaskModel, dialect_name, TaskModel.context_id == "chat-x")
        return " ".join(str(select(ranked).compile(dialect=dialect)).split())

    pg = _sql("postgresql", postgresql.dialect())
    assert "row_number() OVER (PARTITION BY tasks.context_id ORDER BY tasks.last_updated DESC NULLS LAST, tasks.id DESC)" in pg
    assert "rowid" not in pg
    lite = _sql("sqlite", sqlite.dialect())
    assert "row_number() OVER (PARTITION BY tasks.context_id ORDER BY tasks.rowid DESC)" in lite
