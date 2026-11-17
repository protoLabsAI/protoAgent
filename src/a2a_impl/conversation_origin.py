"""Display-only conversation-origin metadata (docs/design/other-conversations.md §8).

Who started an A2A thread — ``caller`` (display name), ``caller_kind``
(``person`` | ``agent``), ``on_behalf_of`` (a person's name) and ``title`` (one line) —
read from the FIRST turn's A2A request metadata and stored on that turn in the task store,
so the Conversations list (S2) can label the thread "From Claude Code for Josh".

NEVER an authorization input: the transport settled who may call before a turn runs; these
only decide how a thread READS. Missing fields stay missing and an unrecognised
``caller_kind`` is dropped — the server never guesses a person. A private turn (incognito or
a ``hidden`` metadata send) stores nothing new.
"""

from __future__ import annotations

#: The single ``Task.metadata`` key the origin dict nests under, kept apart from the turn
#: telemetry (cost, text offsets, fingerprints) the SDK merges onto the same task.
METADATA_KEY = "conversation_origin"

#: A self-reported display name, bound like ``server.a2a._DELEGATED_CALLER_LIMIT``.
_CALLER_MAX = 64
#: A thread title is ONE line, at most 80 chars (spec §4.4).
_TITLE_MAX = 80
#: ``caller_kind`` is one short token; only the two recognised values are kept.
_KIND_MAX = 16
#: The recognised ``caller_kind`` values — the server never guesses a person, so an unrecognised
#: kind is dropped both at write time (``origin_fields``) and on read (``chat_routes`` row emit).
KINDS = ("person", "agent")


def _one_line(text: object, limit: int) -> str:
    """Collapse whitespace to single spaces and clip to ``limit`` with an ellipsis — the
    one-line shape ``server.a2a._one_line`` gives a self-reported caller string (mirrored
    here because ``a2a_impl`` must never import ``server``)."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def origin_fields(md: dict | None) -> dict[str, str]:
    """The display-only origin fields a caller sent on this turn's metadata, each normalised
    to one clipped line; absent fields and an unrecognised ``caller_kind`` are dropped.

    ``{}`` when none are present OR the turn is private (``incognito`` / ``hidden``) — a
    private turn stores no origin."""
    if not isinstance(md, dict) or md.get("incognito") or md.get("hidden"):
        return {}
    out: dict[str, str] = {}
    caller = _one_line(md.get("caller"), _CALLER_MAX)
    if caller:
        out["caller"] = caller
    kind = _one_line(md.get("caller_kind"), _KIND_MAX)
    if kind in KINDS:
        out["caller_kind"] = kind
    on_behalf_of = _one_line(md.get("on_behalf_of"), _CALLER_MAX)
    if on_behalf_of:
        out["on_behalf_of"] = on_behalf_of
    title = _one_line(md.get("title"), _TITLE_MAX)
    if title:
        out["title"] = title
    return out


async def read_origin(engine, context_id: str) -> dict[str, str]:
    """The origin stored on the EARLIEST turn of ``context_id`` that carries any — scanned
    oldest-first, first non-empty wins, so a later turn's metadata never overwrites the
    thread's origin. ``{}`` when the context has no stored origin, or without a task engine
    (a headless test app)."""
    if engine is None or not context_id:
        return {}
    from a2a.server.tasks.database_task_store import TaskModel
    from a2a_impl.stores import task_public_filter
    from sqlalchemy import literal_column, select

    async with engine.begin() as conn:
        # Oldest-CREATED turn first — the reverse of ``stores.task_newest_first``. On SQLite
        # (the only store this runtime builds) ``rowid`` is insertion order; any other
        # dialect falls back to last-change order rather than failing the read.
        if conn.dialect.name == "sqlite":
            order: tuple = (literal_column(f"{TaskModel.__tablename__}.rowid").asc(),)
        else:
            order = (TaskModel.last_updated.asc().nulls_first(), TaskModel.id.asc())
        rows = await conn.execute(
            # ``task_metadata`` is the attribute mapped to the ``metadata`` column —
            # ``TaskModel.metadata`` is SQLAlchemy's own ``MetaData`` object, not the column.
            select(TaskModel.task_metadata).where(TaskModel.context_id == context_id).where(await task_public_filter(conn)).order_by(*order)
        )
        for (meta,) in rows:
            origin = meta.get(METADATA_KEY) if isinstance(meta, dict) else None
            if isinstance(origin, dict):
                kept = {k: v for k, v in origin.items() if isinstance(v, str) and v}
                if kept:
                    return kept
    return {}
