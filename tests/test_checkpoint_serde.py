"""ADR 0120 amendment 1: media offload lives in the serializer, so ANY saver handed
``protoagent_serde(conn)`` — a stock ``SqliteSaver``, LangGraph Studio, a script —
reads byte-identical messages, images included."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph

from graph import checkpoint_prune
from graph.checkpoint_media import MIN_OFFLOAD_CHARS, REF_PREFIX, offload_channel_values
from graph.checkpoint_serde import ProtoagentSerde, protoagent_serde
from graph.checkpointer import build_sqlite_checkpointer


def _url(seed: int) -> str:
    raw = hashlib.sha256(str(seed).encode()).digest() * (3 * MIN_OFFLOAD_CHARS // 32)
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def _img(seed: int) -> ToolMessage:
    return ToolMessage(
        content=[{"type": "text", "text": f"shot {seed}"}, {"type": "image_url", "image_url": {"url": _url(seed)}}],
        tool_call_id=f"c{seed}",
        name="campaign_view",
    )


def _app(saver, seed):
    g = StateGraph(MessagesState)
    g.add_node("n", lambda s: {"messages": [_img(seed[0])]})
    g.add_edge(START, "n")
    g.add_edge("n", END)
    return g.compile(checkpointer=saver)


def _write(db: str, thread: str, seeds) -> None:
    saver = build_sqlite_checkpointer(db)
    seed = [0]
    app = _app(saver, seed)
    cfg = {"configurable": {"thread_id": thread}}

    async def go():
        for s in seeds:
            seed[0] = s
            await app.ainvoke({"messages": [HumanMessage(content=str(s))]}, cfg)

    asyncio.run(go())
    saver.conn.close()


def _image_urls(tup) -> list[str]:
    return [
        m.content[1]["image_url"]["url"]
        for m in tup.checkpoint["channel_values"]["messages"]
        if isinstance(m, ToolMessage)
    ]


def test_a_stock_sqlite_saver_with_the_serde_reads_offloaded_images_exactly(tmp_path):
    db = str(tmp_path / "c.db")
    _write(db, "a2a:t", [1, 2, 3])
    raw = sqlite3.connect(db)
    biggest = raw.execute("SELECT MAX(length(checkpoint)) FROM checkpoints").fetchone()[0]
    raw.close()
    assert biggest < MIN_OFFLOAD_CHARS  # the rows really are slim — the images are offloaded

    conn = sqlite3.connect(db, check_same_thread=False)
    stock = SqliteSaver(conn, serde=protoagent_serde(conn))
    tup = stock.get_tuple({"configurable": {"thread_id": "a2a:t"}})
    assert _image_urls(tup) == [_url(1), _url(2), _url(3)]
    assert all(REF_PREFIX not in u for u in _image_urls(tup))
    # history listing goes through the same serializer
    assert all(REF_PREFIX not in u for t in stock.list({"configurable": {"thread_id": "a2a:t"}}) for u in _image_urls(t))
    conn.close()

    # Without the serde a stock saver sees references — the reason the serde exists.
    conn = sqlite3.connect(db, check_same_thread=False)
    bare = SqliteSaver(conn).get_tuple({"configurable": {"thread_id": "a2a:t"}})
    assert all(u.startswith(REF_PREFIX) for u in _image_urls(bare))
    conn.close()


def test_a_stock_saver_with_the_serde_also_writes_offloaded(tmp_path):
    db = str(tmp_path / "c.db")
    conn = sqlite3.connect(db, check_same_thread=False)
    stock = SqliteSaver(conn, serde=protoagent_serde(conn))
    seed = [7]
    app = _app(stock, seed)
    cfg = {"configurable": {"thread_id": "a2a:s"}}
    app.invoke({"messages": [HumanMessage(content="x")]}, cfg)
    assert conn.execute("SELECT COUNT(*) FROM checkpoint_media").fetchone()[0] == 1
    assert _image_urls(stock.get_tuple(cfg)) == [_url(7)]
    conn.close()


def test_rows_without_references_skip_the_media_lookup(tmp_path, monkeypatch):
    from graph import checkpoint_serde

    db = str(tmp_path / "c.db")
    conn = sqlite3.connect(db)
    serde = protoagent_serde(conn)
    calls = []
    monkeypatch.setattr(checkpoint_serde, "fetch_media", lambda *a: calls.append(a) or {})
    blob = serde.dumps_typed({"channel_values": {"messages": [HumanMessage(content="hi")]}})
    assert serde.loads_typed(blob)["channel_values"]["messages"][0].content == "hi"
    assert calls == []
    conn.close()


def test_with_allowlist_keeps_the_serde_and_its_connection(tmp_path):
    saver = build_sqlite_checkpointer(str(tmp_path / "c.db"))
    clone = saver.with_allowlist([("my_mod", "MyType")])
    assert isinstance(clone.serde, ProtoagentSerde)
    assert clone.serde.conn is saver.conn


def test_per_thread_blobs_from_4306_migrate_into_per_db_media(tmp_path):
    """#4306 stored payloads per thread in ``checkpoint_blobs``. Opening the DB folds them
    into ``checkpoint_media`` (deduped across threads), drops the old table, and the old
    rows' references keep resolving."""
    db = str(tmp_path / "c.db")
    conn = sqlite3.connect(db, check_same_thread=False)
    SqliteSaver(conn).setup()
    conn.execute(
        "CREATE TABLE checkpoint_blobs (thread_id TEXT NOT NULL, hash TEXT NOT NULL, data BLOB NOT NULL,"
        " created REAL NOT NULL DEFAULT 0, PRIMARY KEY (thread_id, hash))"
    )
    plain = SqliteSaver(conn)  # writes exactly what #4306's saver wrote: refs in, payloads beside
    for tid in ("a2a:a", "a2a:b"):  # the same image in two chats → two legacy rows, one hash
        blobs: dict[str, str] = {}
        cv = offload_channel_values({"messages": [HumanMessage(content="hi"), _img(5)]}, blobs)
        for h, data in blobs.items():
            conn.execute(
                "INSERT INTO checkpoint_blobs VALUES (?, ?, ?, ?)", (tid, h, data.encode(), 1.0)
            )
        conn.commit()
        cfg = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
        ck = {"v": 1, "id": "1f1c0000-0000-6000-8000-00000000000" + tid[-1].replace("a", "1").replace("b", "2"),
              "ts": "2026-10-16T00:00:00+00:00", "channel_values": cv, "channel_versions": {},
              "versions_seen": {}, "pending_sends": []}
        plain.put(cfg, ck, {"source": "loop", "step": 1, "parents": {}}, {})
    conn.close()

    saver = build_sqlite_checkpointer(db)
    tables = {r[0] for r in saver.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "checkpoint_blobs" not in tables and "checkpoint_media" in tables
    assert saver.conn.execute("SELECT COUNT(*) FROM checkpoint_media").fetchone()[0] == 1
    for tid in ("a2a:a", "a2a:b"):
        assert _image_urls(saver.get_tuple({"configurable": {"thread_id": tid}})) == [_url(5)]
    # idempotent: a second open is a no-op
    build_sqlite_checkpointer(db)


def test_gc_spares_a_payload_a_row_referenced_while_it_ran(tmp_path, monkeypatch):
    """Phase 2 rescans rows written after phase 1 began (rowid above the mark), so a row
    that commits a reference mid-GC keeps its payload."""
    db = str(tmp_path / "c.db")
    _write(db, "a2a:t", [1])
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM checkpoints")
    conn.execute("DELETE FROM writes")
    conn.commit()
    (h,) = conn.execute("SELECT hash FROM checkpoint_media").fetchone()
    conn.close()

    real = checkpoint_prune._scan_refs
    calls = []

    def _racing(c, *, after=None):
        found = real(c, after=after)
        if after is None and not calls:
            calls.append(1)
            w = sqlite3.connect(db)
            w.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)"
                " VALUES ('a2a:new', '', 'x', 'msgpack', ?, '{}')",
                ((REF_PREFIX + h).encode(),),
            )
            w.commit()
            w.close()
        return found

    monkeypatch.setattr(checkpoint_prune, "_scan_refs", _racing)
    assert checkpoint_prune.gc_media(db, grace_s=0) == 0
    monkeypatch.setattr(checkpoint_prune, "_scan_refs", real)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM checkpoints")
    conn.commit()
    conn.close()
    assert checkpoint_prune.gc_media(db, grace_s=0) == 1


def test_serde_is_a_jsonplus_serializer():
    assert issubclass(ProtoagentSerde, JsonPlusSerializer)
