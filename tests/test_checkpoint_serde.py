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
    """Phase 2 rescans every row under the write lock, so a row that commits a reference
    between the phases keeps its payload — wherever its rowid lands (a rowid-mark
    partial rescan missed rows that reused a deleted max rowid, #4313 review m1)."""
    db = str(tmp_path / "c.db")
    _write(db, "a2a:t", [1])
    conn = sqlite3.connect(db)
    (h,) = conn.execute("SELECT hash FROM checkpoint_media").fetchone()
    top = conn.execute("SELECT MAX(rowid) FROM checkpoints").fetchone()[0]
    conn.execute("DELETE FROM writes")
    conn.execute("DELETE FROM checkpoints WHERE rowid < ?", (top,))
    conn.execute("UPDATE checkpoints SET checkpoint = x'00' WHERE rowid = ?", (top,))  # unreferenced now
    conn.commit()
    conn.close()

    real = checkpoint_prune._scan_refs
    calls = []

    def _racing(c):
        found = real(c)
        if not calls:
            calls.append(1)
            w = sqlite3.connect(db)
            w.execute("DELETE FROM checkpoints WHERE rowid = ?", (top,))  # the max rowid goes…
            w.execute(  # …and a new referencing row lands at or below the old max
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)"
                " VALUES ('a2a:new', '', 'x', 'msgpack', ?, '{}')",
                ((REF_PREFIX + h).encode(),),
            )
            assert w.execute("SELECT rowid FROM checkpoints WHERE thread_id='a2a:new'").fetchone()[0] <= top
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


def test_a_put_reusing_a_payload_holds_off_the_gc_until_its_row_commits(tmp_path, monkeypatch):
    """The dedupe hit and the row commit share one write transaction: a GC that starts
    after the existence check waits for the row, then sees it (and keeps the payload)."""
    import threading

    from graph import checkpoint_serde

    db = str(tmp_path / "c.db")
    _write(db, "a2a:old", [4])  # image 4 stored…
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM checkpoints")  # …then unreferenced and old
    conn.execute("DELETE FROM writes")
    conn.execute("UPDATE checkpoint_media SET created = 0")
    conn.commit()
    conn.close()

    stored, release = threading.Event(), threading.Event()
    real_store = checkpoint_serde.store_media

    def _slow_store(c, blobs):
        if not blobs:  # text-only puts (the input step) pass straight through
            return real_store(c, blobs)
        real_store(c, blobs)  # existence check + BEGIN IMMEDIATE done; row not yet written
        stored.set()
        release.wait(5)

    monkeypatch.setattr(checkpoint_serde, "store_media", _slow_store)
    errors: list[BaseException] = []

    def _turn():
        try:
            _write(db, "a2a:new", [4])  # re-sends the same image
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=_turn)
    t.start()
    assert stored.wait(5)
    gc_result: list[int] = []
    g = threading.Thread(target=lambda: gc_result.append(checkpoint_prune.gc_media(db, grace_s=0)))
    g.start()
    release.set()
    t.join(10)
    g.join(10)
    assert not errors and gc_result == [0]
    conn = sqlite3.connect(db, check_same_thread=False)
    tup = SqliteSaver(conn, serde=protoagent_serde(conn)).get_tuple({"configurable": {"thread_id": "a2a:new"}})
    assert _image_urls(tup)[-1] == _url(4)
    conn.close()


def test_concurrent_writers_on_one_saver_never_fail(tmp_path):
    """#4313 review B1: SqliteSaver.put serializes BEFORE taking its lock; with media SQL in
    the serializer that raced other threads' transactions on the shared connection."""
    import concurrent.futures as cf

    db = str(tmp_path / "c.db")
    saver = build_sqlite_checkpointer(db)

    def _chat(i: int):
        seed = [i]
        app = _app(saver, seed)
        cfg = {"configurable": {"thread_id": f"a2a:c{i}"}}
        for k in range(15):
            seed[0] = (i * 100 + k) if i % 2 else 0  # odd chats: new images; even: text-ish reuse
            app.invoke({"messages": [HumanMessage(content=str(k))]}, cfg)

    with cf.ThreadPoolExecutor(6) as ex:
        list(ex.map(_chat, range(6)))  # raises on any failed write
    for i in range(6):
        tup = saver.get_tuple({"configurable": {"thread_id": f"a2a:c{i}"}})
        assert all(REF_PREFIX not in u for u in _image_urls(tup))


def test_a_failed_media_gc_does_not_fail_the_chat_delete(tmp_path, monkeypatch):
    db = str(tmp_path / "c.db")
    _write(db, "a2a:x", [9])

    def _locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(checkpoint_prune, "gc_media", _locked)
    assert checkpoint_prune.delete_thread(db, "a2a:x", cascade=True) > 0  # no raise


def test_media_table_setup_never_raises(tmp_path, caplog):
    """A locked DB at open must not make the checkpointer fall back to in-memory history."""
    from graph.checkpoint_serde import ensure_media_table

    db = str(tmp_path / "c.db")
    holder = sqlite3.connect(db)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN EXCLUSIVE")
    conn = sqlite3.connect(db, timeout=0.1)
    ensure_media_table(conn)  # logs, doesn't raise
    assert "could not create/migrate" in caplog.text
    holder.rollback()
    holder.close()
    ensure_media_table(conn)  # next open succeeds
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='checkpoint_media'").fetchone()
    conn.close()


def test_serde_is_a_jsonplus_serializer():
    assert issubclass(ProtoagentSerde, JsonPlusSerializer)
