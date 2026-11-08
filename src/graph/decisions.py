"""Durable "decision needed" ledger (#4725, item 1).

When the agent reaches a choice it is not allowed to make on its own — a skill rule
forbids proceeding on judgment, a destructive or outward-facing step, an operator-only
call — it calls ``request_decision`` (``tools/lg_tools.py``). On an UNATTENDED turn
(scheduler / inbox / background) every other HITL interrupt auto-answers so the turn can't
deadlock (``server/turn_control.py``); a ``request_decision`` pause is the ONE exception
(ADR 0070, this card): the turn parks and waits for a person, because the agent chose to
stop rather than guess.

A parked decision therefore has to outlive the turn's checkpoint — the ``input-required``
A2A task is TTL-exempt, but the console needs a durable, queryable handle independent of
it. That handle is a record here: question, options, the session it belongs to, the agent
that asked, when. The operator answers it over ``GET/POST /api/decisions``
(``operator_api/decision_routes.py``), which resumes the parked turn.

Storage is one SQLite table under the per-instance data dir (ADR 0004/0065), mirroring the
other durable stores (``inbox/store.py``). Every state change publishes
``decision.changed {id, session_id, state}`` on the bus (ADR 0039, the same pattern as
``directive.changed``) so a console view refreshes without polling.

This is item 1 of the build; card 2 adds expiry (``expires_at`` / ``default_option``,
stored-but-unused here) and notifications, card 3 the console view.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("protoagent.decisions")

STATE_PENDING = "pending"
STATE_ANSWERED = "answered"
STATE_EXPIRED = "expired"
STATE_CANCELLED = "cancelled"
STATES = (STATE_PENDING, STATE_ANSWERED, STATE_EXPIRED, STATE_CANCELLED)

#: A question is a sentence or two, not a pasted essay; bounded so one record can't bloat
#: the store or the inbox render. Options are short labels.
MAX_QUESTION_CHARS = 4000
MAX_LABEL_CHARS = 400
MAX_ANSWER_CHARS = 4000
MAX_OPTIONS = 25

# One lock for every store object: :func:`decision_store` may hand out a fresh one per call,
# and the SQLite file is shared; serialize the read-modify-write of answer/cancel so two
# store objects over the same db can't race a non-pending transition.
_LOCK = threading.RLock()


class DecisionError(ValueError):
    """A decision operation the caller can correct (empty question, unknown id, already
    answered, unknown option)."""


class DecisionNotFound(DecisionError):
    """No decision with that id."""


class DecisionClosed(DecisionError):
    """The decision is no longer pending (already answered / cancelled / expired)."""


def normalize_options(raw) -> list[dict]:
    """Shape the tool's ``list[str | dict]`` into ``[{id, label, description?}]``.

    A bare string becomes ``{"id": s, "label": s}``; a dict carries its own ``id`` /
    ``label`` / ``description`` (``label`` defaults to ``id`` and vice-versa). Ids are made
    unique (a ``-2`` suffix on a clash) so an answer can address exactly one option, and
    labels/descriptions are clipped. Anything that isn't a string or a mappable dict is
    skipped. Bounded to :data:`MAX_OPTIONS`."""
    out: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(list(raw or [])[:MAX_OPTIONS]):
        desc = None
        if isinstance(item, str):
            label = item.strip()
            oid = label
        elif isinstance(item, dict):
            label = str(item.get("label") or item.get("id") or "").strip()
            oid = str(item.get("id") or item.get("label") or "").strip()
            raw_desc = item.get("description")
            desc = str(raw_desc).strip() if raw_desc else None
        else:
            continue
        label = (label or oid)[:MAX_LABEL_CHARS]
        oid = (oid or label or f"option-{i + 1}")[:MAX_LABEL_CHARS]
        base = oid
        n = 2
        while oid in seen:
            oid = f"{base}-{n}"
            n += 1
        seen.add(oid)
        entry = {"id": oid, "label": label or oid}
        if desc:
            entry["description"] = desc[:MAX_LABEL_CHARS]
        out.append(entry)
    return out


@dataclass
class Decision:
    id: str
    session_id: str
    agent: str
    question: str
    options: list[dict] = field(default_factory=list)
    created_at: str = ""
    state: str = STATE_PENDING
    answer: str | None = None
    answered_at: str | None = None
    # Card 2 fills these in (expiry + a default the sweep applies); stored-but-unused here.
    expires_at: str | None = None
    default_option: str | None = None
    thread_id: str = ""
    interrupt_id: str = ""
    context: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def pending(self) -> bool:
        return self.state == STATE_PENDING

    def option_ids(self) -> list[str]:
        return [str(o.get("id")) for o in self.options if isinstance(o, dict) and o.get("id")]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Decision:
        try:
            options = json.loads(row["options"]) if row["options"] else []
        except (TypeError, ValueError):
            options = []
        return cls(
            id=str(row["id"]),
            session_id=str(row["session_id"] or ""),
            agent=str(row["agent"] or ""),
            question=str(row["question"] or ""),
            options=options if isinstance(options, list) else [],
            created_at=str(row["created_at"] or ""),
            state=str(row["state"] or STATE_PENDING),
            answer=row["answer"],
            answered_at=row["answered_at"],
            expires_at=row["expires_at"],
            default_option=row["default_option"],
            thread_id=row["thread_id"],
            interrupt_id=row["interrupt_id"],
            context=row["context"],
        )


def _publish(topic: str, data: dict) -> None:
    """Best-effort bus push so a console view can refresh on a change. No-op when the host
    hasn't wired a publisher; a bus hiccup must never break a ledger write."""
    try:
        from graph.plugins.host import HOST

        if HOST.publish:
            HOST.publish(topic, data)
    except Exception:  # noqa: BLE001
        pass


def _resolve_db_path() -> Path:
    from infra.paths import instance_paths

    return instance_paths().store("decisions") / "decisions.db"


class DecisionStore:
    """The durable decisions ledger. One SQLite table; thread-safe via the module lock
    (one process, low contention) plus SQLite's own busy-timeout for cross-process safety.

    ``clock`` is an injectable ``() -> datetime`` (default wall-clock UTC) so tests pin a
    fake clock; ``db_path`` defaults to ``<instance_root>/decisions/decisions.db`` (an
    in-memory or tmp path in tests)."""

    def __init__(self, db_path: str | Path | None = None, *, clock: Callable[[], datetime] | None = None):
        self.path = str(db_path) if db_path else str(_resolve_db_path())
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = _LOCK
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._shared: sqlite3.Connection | None = None
        self._init_db()

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _connect(self) -> sqlite3.Connection:
        # An in-memory db lives only in its connection, so a fresh connect per call would
        # see an empty schema — hold ONE shared connection for ``:memory:`` (tests).
        if self.path == ":memory:":
            if self._shared is None:
                # ``check_same_thread=False``: a route reads the store via ``asyncio.to_thread``
                # (a worker thread), so the one shared in-memory connection must cross threads.
                # The module lock serializes the read-modify-write transitions.
                self._shared = sqlite3.connect(self.path, check_same_thread=False)
                self._shared.row_factory = sqlite3.Row
            return self._shared
        db = sqlite3.connect(self.path)
        # busy_timeout FIRST (the WAL pragma takes a lock) — mirrors inbox/store.py.
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row
        return db

    def _close(self, db: sqlite3.Connection) -> None:
        if db is not self._shared:
            db.close()

    def _init_db(self) -> None:
        db = self._connect()
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id             TEXT PRIMARY KEY,
                    session_id     TEXT NOT NULL,
                    agent          TEXT,
                    question       TEXT NOT NULL,
                    options        TEXT NOT NULL,
                    created_at     TEXT NOT NULL,
                    state          TEXT NOT NULL,
                    answer         TEXT,
                    answered_at    TEXT,
                    expires_at     TEXT,
                    default_option TEXT
                )
                """
            )
            columns = {r[1] for r in db.execute("PRAGMA table_info(decisions)")}
            for column in ("thread_id", "interrupt_id", "context"):
                if column not in columns:
                    db.execute(f"ALTER TABLE decisions ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS ix_decisions_state ON decisions(state, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_decisions_session ON decisions(session_id)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_decisions_interrupt ON decisions(thread_id, interrupt_id)")
            db.commit()
        finally:
            self._close(db)

    # ── operations ──────────────────────────────────────────────────────────────

    def create(
        self,
        session_id: str,
        question: str,
        options,
        *,
        agent: str = "",
        thread_id: str = "",
        interrupt_id: str = "",
        context: str = "",
        default_option: str | None = None,
        expires_at: str | None = None,
    ) -> Decision:
        """Record a new pending decision. ``options`` is the tool's raw ``list[str | dict]``
        (normalized here). Publishes ``decision.changed`` with ``state="pending"``."""
        session_id = (session_id or "").strip()
        if not session_id:
            raise DecisionError("a decision belongs to a chat — no session id was given")
        question = (question or "").strip()
        if not question:
            raise DecisionError("question is required")
        question = question[:MAX_QUESTION_CHARS]
        opts = normalize_options(options)
        default_option = (str(default_option).strip() or None) if default_option else None
        with self._lock:
            db = self._connect()
            try:
                if interrupt_id:
                    row = db.execute(
                        "SELECT * FROM decisions WHERE thread_id = ? AND interrupt_id = ?",
                        (thread_id, interrupt_id),
                    ).fetchone()
                    if row is not None:
                        return Decision.from_row(row)
                new_id = f"dec-{secrets.token_hex(4)}"
                while db.execute("SELECT 1 FROM decisions WHERE id = ?", (new_id,)).fetchone() is not None:
                    new_id = f"dec-{secrets.token_hex(4)}"
                rec = Decision(
                    id=new_id,
                    thread_id=thread_id,
                    interrupt_id=interrupt_id,
                    context=str(context or "")[:MAX_QUESTION_CHARS],
                    session_id=session_id,
                    agent=(agent or "").strip(),
                    question=question,
                    options=opts,
                    created_at=self._now(),
                    state=STATE_PENDING,
                    expires_at=expires_at,
                    default_option=default_option,
                )
                db.execute(
                    "INSERT INTO decisions (id, session_id, agent, question, options, created_at, state, "
                    "answer, answered_at, expires_at, default_option) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec.id,
                        rec.session_id,
                        rec.agent,
                        rec.question,
                        json.dumps(rec.options),
                        rec.created_at,
                        rec.state,
                        rec.answer,
                        rec.answered_at,
                        rec.expires_at,
                        rec.default_option,
                    ),
                )
                db.execute(
                    "UPDATE decisions SET thread_id = ?, interrupt_id = ?, context = ? WHERE id = ?",
                    (rec.thread_id, rec.interrupt_id, rec.context, rec.id),
                )
                db.commit()
            finally:
                self._close(db)
        _publish("decision.changed", {"id": rec.id, "session_id": rec.session_id, "state": rec.state})
        return rec

    def get(self, decision_id: str) -> Decision | None:
        decision_id = (decision_id or "").strip()
        if not decision_id:
            return None
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        finally:
            self._close(db)
        return Decision.from_row(row) if row is not None else None

    def for_interrupt(self, thread_id: str, interrupt_id: str) -> Decision | None:
        """Find the durable handle for one checkpoint interrupt, regardless of state."""
        db = self._connect()
        try:
            row = db.execute(
                "SELECT * FROM decisions WHERE thread_id = ? AND interrupt_id = ?",
                (thread_id, interrupt_id),
            ).fetchone()
        finally:
            self._close(db)
        return Decision.from_row(row) if row is not None else None

    def list(self, *, state: str | None = None, session_id: str | None = None, limit: int = 200) -> list[Decision]:
        """Decisions newest first, optionally filtered by ``state`` and/or ``session_id``."""
        where = []
        params: list = []
        if state:
            where.append("state = ?")
            params.append(state)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id.strip())
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        db = self._connect()
        try:
            rows = db.execute(
                f"SELECT * FROM decisions{clause} ORDER BY created_at DESC, id DESC LIMIT ?",
                (*params, max(1, int(limit))),
            ).fetchall()
        finally:
            self._close(db)
        return [Decision.from_row(r) for r in rows]

    def answer(self, decision_id: str, answer: str) -> Decision:
        """Mark a pending decision answered with the operator's choice. Raises
        :class:`DecisionNotFound` for an unknown id and :class:`DecisionClosed` when it is
        no longer pending (answering twice is refused). Publishes ``decision.changed``."""
        return self._close_record(decision_id, STATE_ANSWERED, answer=answer)

    def cancel(self, decision_id: str) -> Decision:
        """Cancel a pending decision (the operator dismissed it, or its turn was abandoned).
        Refused when it is no longer pending. Publishes ``decision.changed``."""
        return self._close_record(decision_id, STATE_CANCELLED)

    def _close_record(self, decision_id: str, state: str, *, answer: str | None = None) -> Decision:
        decision_id = (decision_id or "").strip()
        with self._lock:
            db = self._connect()
            try:
                row = db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
                if row is None:
                    raise DecisionNotFound(f"no decision {decision_id!r}")
                rec = Decision.from_row(row)
                if not rec.pending:
                    raise DecisionClosed(f"decision {decision_id} is already {rec.state}")
                rec.state = state
                if answer is not None:
                    rec.answer = str(answer)[:MAX_ANSWER_CHARS]
                    rec.answered_at = self._now()
                db.execute(
                    "UPDATE decisions SET state = ?, answer = ?, answered_at = ? WHERE id = ? AND state = ?",
                    (rec.state, rec.answer, rec.answered_at, rec.id, STATE_PENDING),
                )
                db.commit()
            finally:
                self._close(db)
        _publish("decision.changed", {"id": rec.id, "session_id": rec.session_id, "state": rec.state})
        return rec


def decision_store() -> DecisionStore:
    """The live store: ``STATE.decision_store`` (built once at boot — the instance root
    never changes for the life of a process), else a store over this instance's directory
    (a pre-setup console, a test). Not cached on STATE here: a lazily cached store would pin
    whatever instance root was current then (mirrors ``directives.directive_store``)."""
    from runtime.state import STATE

    store = getattr(STATE, "decision_store", None)
    return store if store is not None else DecisionStore()
