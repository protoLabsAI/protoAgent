"""Media offload as a LangGraph serializer (ADR 0120, amendment 1).

#4306 offloaded large images out of checkpoint rows inside ``ThreadedSqliteSaver``, so
only that saver could read them back: a stock ``SqliteSaver``, LangGraph Studio or a
one-off script saw ``protoagent-ckblob:`` references where the images had been. Here
the same offload lives in the serializer, which every saver calls on both paths::

    from langgraph.checkpoint.sqlite import SqliteSaver
    from graph.checkpoint_serde import protoagent_serde

    saver = SqliteSaver(conn, serde=protoagent_serde(conn))  # byte-identical messages

* :meth:`ProtoagentSerde.dumps_typed` lifts large base64 / ``data:`` strings out of
  message content blocks (``graph.checkpoint_media``), stores each payload ONCE per DB
  in ``checkpoint_media`` (content-addressed — a serializer has no thread context, and
  the same screenshot in two chats is one row), then serializes the slimmed object.
* :meth:`ProtoagentSerde.loads_typed` deserializes, then restores the payloads. Rows
  with no reference (a byte check, no walk) cost nothing extra.

It subclasses ``JsonPlusSerializer`` so LangGraph's ``with_allowlist`` keeps working
(``with_msgpack_allowlist`` ``copy.copy``s the instance, connection included).

Payloads are freed by :func:`graph.checkpoint_prune.gc_media` (reference scan with a
grace window), which also runs right after a chat's rows are deleted.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from graph.checkpoint_media import (
    REF_PREFIX,
    offload_channel_values,
    offload_value,
    refs_in_value,
    rehydrate_value,
)

log = logging.getLogger(__name__)

MEDIA_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS checkpoint_media ("
    " hash TEXT PRIMARY KEY,"
    " data BLOB NOT NULL,"
    " created REAL NOT NULL DEFAULT 0)"
)

_REF_BYTES = REF_PREFIX.encode()


def ensure_media_table(conn: sqlite3.Connection) -> None:
    """Create ``checkpoint_media`` and fold in #4306's per-thread ``checkpoint_blobs``
    (same payloads, keyed ``(thread_id, hash)``): one row per hash, the newest
    ``created`` kept, then the old table dropped. Idempotent; one transaction."""
    conn.execute(MEDIA_TABLE_DDL)
    legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoint_blobs'"
    ).fetchone()
    if legacy:
        conn.execute(
            "INSERT OR IGNORE INTO checkpoint_media (hash, data, created)"
            " SELECT hash, data, MAX(created) FROM checkpoint_blobs GROUP BY hash"
        )
        conn.execute("DROP TABLE checkpoint_blobs")
        log.info("[checkpoint-serde] migrated per-thread checkpoint_blobs into checkpoint_media")
    conn.commit()


def store_media(conn: sqlite3.Connection, blobs: dict[str, str]) -> None:
    """Insert payloads not already stored. Never commits: the saver's own commit right
    after (``put`` / ``put_writes`` both serialize, then write and commit on this same
    connection) covers it, and the payload ``INSERT`` always executes before the row's,
    so a row can't commit without its payloads. Committing here raced other threads'
    commits on the shared connection ("cannot commit - no transaction is active")."""
    if not blobs:
        return
    hashes = list(blobs)
    have: set[str] = set()
    for k in range(0, len(hashes), 500):
        chunk = hashes[k : k + 500]
        q = f"SELECT hash FROM checkpoint_media WHERE hash IN ({','.join('?' * len(chunk))})"
        have.update(r[0] for r in conn.execute(q, chunk))
    now = time.time()
    conn.executemany(
        "INSERT OR IGNORE INTO checkpoint_media (hash, data, created) VALUES (?, ?, ?)",
        [(h, blobs[h].encode("utf-8", "surrogatepass"), now) for h in hashes if h not in have],
    )


def fetch_media(conn: sqlite3.Connection, hashes: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    hs = sorted(hashes)
    for k in range(0, len(hs), 500):
        chunk = hs[k : k + 500]
        q = f"SELECT hash, data FROM checkpoint_media WHERE hash IN ({','.join('?' * len(chunk))})"
        out.update((h, bytes(d).decode("utf-8", "surrogatepass")) for h, d in conn.execute(q, chunk))
    return out


def _offload_obj(obj: Any, blobs: dict[str, str]) -> Any:
    # A checkpoint is a dict holding ``channel_values``; a pending write's value is the
    # channel value itself (a message, or a list of them).
    if isinstance(obj, dict) and isinstance(obj.get("channel_values"), dict):
        cv = obj["channel_values"]
        new_cv = offload_channel_values(cv, blobs)
        return obj if new_cv is cv else {**obj, "channel_values": new_cv}
    return offload_value(obj, blobs)


def _refs_obj(obj: Any, found: set[str]) -> None:
    if isinstance(obj, dict) and isinstance(obj.get("channel_values"), dict):
        for v in obj["channel_values"].values():
            refs_in_value(v, found)
    else:
        refs_in_value(obj, found)


def _rehydrate_obj(obj: Any, payloads: dict[str, str]) -> Any:
    if isinstance(obj, dict) and isinstance(obj.get("channel_values"), dict):
        cv = obj["channel_values"]
        return {**obj, "channel_values": {k: rehydrate_value(v, payloads) for k, v in cv.items()}}
    return rehydrate_value(obj, payloads)


class ProtoagentSerde(JsonPlusSerializer):
    """``JsonPlusSerializer`` that keeps large message media out of the serialized bytes,
    in this DB's ``checkpoint_media`` table. ``offload=False`` stops offloading new
    writes; reading always restores (a DB written with offload on stays readable)."""

    def __init__(self, conn: sqlite3.Connection, *, offload: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.conn = conn
        self.offload = offload
        ensure_media_table(conn)

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        if self.offload:
            blobs: dict[str, str] = {}
            try:
                slim = _offload_obj(obj, blobs)
                # Payloads BEFORE the row that references them: a crash in between leaves
                # an unreferenced payload (GC'd), never a dangling reference.
                store_media(self.conn, blobs)
                obj = slim
            except Exception:  # noqa: BLE001 — fail OPEN: serialize inline, never fail the write
                log.warning("[checkpoint-serde] media offload failed; serializing inline", exc_info=True)
        return super().dumps_typed(obj)

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        obj = super().loads_typed(data)
        raw = data[1] if len(data) > 1 else None
        if not isinstance(raw, (bytes, bytearray, memoryview)) or _REF_BYTES not in bytes(raw):
            return obj
        found: set[str] = set()
        _refs_obj(obj, found)
        if not found:
            return obj
        return _rehydrate_obj(obj, fetch_media(self.conn, found))


def protoagent_serde(conn: sqlite3.Connection, *, offload: bool = True, **kwargs: Any) -> ProtoagentSerde:
    """The serializer to hand any LangGraph saver on a protoAgent checkpoint DB:
    ``SqliteSaver(conn, serde=protoagent_serde(conn))``."""
    return ProtoagentSerde(conn, offload=offload, **kwargs)
