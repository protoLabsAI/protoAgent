"""ADR 0120: a long, image-heavy turn can't grow a checkpoint thread without bound.

The incident: one visual-review turn on brandLaunch wrote ~2,200 checkpoints of ~55 MB
each in five hours (87 GB) — every super-step re-serialized ~490 base64 screenshots,
and the 6-hourly prune sweep was the only bound. These tests pin the write-path bounds
(retention on write, media offload), the reclaim drain, the size alerts and the
dry-run-first compaction.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from graph import checkpoint_health
from graph.checkpoint_media import MIN_OFFLOAD_CHARS, REF_PREFIX, UNAVAILABLE_TEXT, offload_value, rehydrate_value
from graph.checkpoint_prune import (
    compact,
    delete_thread,
    gc_media,
    plan_compaction,
    prune_checkpoints,
    reclaim,
    trim_group,
)
from graph.checkpointer import build_sqlite_checkpointer, configure_saver


def _png(seed: int, size: int = 3 * MIN_OFFLOAD_CHARS) -> str:
    """A unique base64 payload bigger than the offload threshold."""
    raw = hashlib.sha256(str(seed).encode()).digest() * (size // 32 + 1)
    return base64.b64encode(raw[: size * 3 // 4]).decode()


def _image_msg(seed: int) -> ToolMessage:
    return ToolMessage(
        content=[
            {"type": "text", "text": f"asset #{seed}"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _png(seed)}},
        ],
        tool_call_id=f"call-{seed}",
        name="campaign_view",
    )


def _visual_graph(saver, image_seed: list[int]):
    """Three super-steps per turn (look → tool result with an image → answer), the
    shape of a screenshot-review loop."""
    g = StateGraph(MessagesState)
    g.add_node("look", lambda s: {"messages": [AIMessage(content="looking")]})
    g.add_node("tool", lambda s: {"messages": [_image_msg(image_seed[0])]})
    g.add_node("answer", lambda s: {"messages": [AIMessage(content="looks good")]})
    g.add_edge(START, "look")
    g.add_edge("look", "tool")
    g.add_edge("tool", "answer")
    g.add_edge("answer", END)
    return g.compile(checkpointer=saver)


def _rows(db, table, thread_id):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", (thread_id,)).fetchone()[0]
    finally:
        conn.close()


def _media(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM checkpoint_media").fetchone()[0]
    finally:
        conn.close()


def _max_checkpoint_bytes(db, thread_id):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT MAX(length(checkpoint)) FROM checkpoints WHERE thread_id=?", (thread_id,)).fetchone()[0]
    finally:
        conn.close()


def _run_turns(db, turns, *, seeds=None, prune_on_write=True, offload_media=True, thread="a2a:visual"):
    saver = build_sqlite_checkpointer(db)
    saver.prune_on_write = prune_on_write
    saver.offload_media = offload_media
    seed = [0]
    app = _visual_graph(saver, seed)
    cfg = {"configurable": {"thread_id": thread}}

    async def main():
        for i in range(turns):
            seed[0] = seeds[i] if seeds else i
            await app.ainvoke({"messages": [HumanMessage(content=f"turn {i}")]}, cfg)
        return await app.aget_state(cfg)

    return saver, asyncio.run(main())


# ── media offload ─────────────────────────────────────────────────────────────


def test_offloaded_images_round_trip_byte_identical(tmp_path):
    """The graph (and so the model and console) reads back exactly the images it wrote."""
    db = str(tmp_path / "c.db")
    _, state = _run_turns(db, 3)
    tools = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
    assert [m.content[1]["image_url"]["url"] for m in tools] == [
        "data:image/png;base64," + _png(i) for i in range(3)
    ]
    assert all(REF_PREFIX not in json.dumps(m.content) for m in tools)


def test_checkpoint_rows_do_not_carry_images(tmp_path):
    """With offload, a checkpoint row is a few KB however many images the thread holds;
    without it, every row carries every image (the incident's quadratic growth)."""
    on, off = str(tmp_path / "on.db"), str(tmp_path / "off.db")
    _run_turns(on, 6)
    _run_turns(off, 6, offload_media=False)
    image_bytes = len(_png(0))
    assert _max_checkpoint_bytes(off, "a2a:visual") > 5 * image_bytes
    assert _max_checkpoint_bytes(on, "a2a:visual") < image_bytes // 4


def test_the_same_image_is_stored_once_per_db(tmp_path):
    """Content-addressed per DB: one screenshot re-read in many turns — or seen by two
    chats — costs one media row."""
    db = str(tmp_path / "c.db")
    _run_turns(db, 5, seeds=[7, 7, 7, 8, 8])
    assert _media(db) == 2
    _run_turns(db, 2, seeds=[7, 8], thread="a2a:other")
    assert _media(db) == 2


def test_offload_never_mutates_the_live_state():
    m = _image_msg(1)
    url = m.content[1]["image_url"]["url"]
    blobs: dict[str, str] = {}
    out = offload_value([m], blobs)
    assert m.content[1]["image_url"]["url"] == url  # the input is untouched
    assert out[0].content[1]["image_url"]["url"].startswith(REF_PREFIX)
    assert list(blobs.values()) == [url]
    small = HumanMessage(content=[{"type": "text", "text": "x" * (MIN_OFFLOAD_CHARS * 2)}])
    assert offload_value([small], {})[0] is small  # long prose is not media


def test_a_missing_payload_degrades_to_text_not_a_broken_image():
    blobs: dict[str, str] = {}
    out = offload_value([_image_msg(2)], blobs)
    back = rehydrate_value(out, {})  # payload gone
    assert back[0].content[1] == {"type": "text", "text": UNAVAILABLE_TEXT}


# ── retention on write ────────────────────────────────────────────────────────


def test_retention_on_write_bounds_a_long_turn_without_the_sweep(tmp_path):
    """No sweep runs, yet the thread never holds more than ``keep`` checkpoints — and
    resuming still sees the whole history."""
    db = str(tmp_path / "c.db")
    saver, state = _run_turns(db, 10)
    assert _rows(db, "checkpoints", "a2a:visual") == saver.keep_per_thread == 2
    assert len(state.values["messages"]) == 10 * 4
    # Only images some surviving row still references are worth keeping; the GC agrees.
    gc_media(db, grace_s=0)
    assert _media(db) == 10


def test_configure_saver_applies_live_settings(tmp_path):
    saver = build_sqlite_checkpointer(str(tmp_path / "c.db"))

    class Cfg:
        checkpoint_keep_per_thread = 4
        checkpoint_background_keep = 2
        checkpoint_prune_on_write = False
        checkpoint_offload_media = False

    configure_saver(saver, Cfg())
    assert (saver.keep_per_thread, saver.background_keep, saver.prune_on_write, saver.offload_media) == (
        4,
        2,
        False,
        False,
    )


def _uuid6(i: int) -> str:
    # Monotonic, sortable ids are all trim_group relies on.
    return str(uuid.UUID(int=(0x1F1C0000_0000_6000_8000_000000000000 + i)))


def _insert(conn, thread_id, cid, *, subagent=False):
    md = {"source": "loop", "step": 1, "parents": {"": "parent-root"} if subagent else {}}
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata)"
        " VALUES (?,?,?,?,?,?,?)",
        (thread_id, "", cid, None, "msgpack", b"x", json.dumps(md).encode()),
    )
    conn.execute(
        "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (thread_id, "", cid, "t", 0, "messages", "msgpack", b"y"),
    )


def test_trim_keeps_the_parent_root_while_a_subagent_writes_into_its_thread(tmp_path):
    """``task()`` subagents write into the parent's (thread, ns). A plain newest-N would
    delete the parent's resumable checkpoint mid-delegation; the trim keeps the newest
    ``keep`` ROOT rows and everything after the oldest of them."""
    db = str(tmp_path / "c.db")
    build_sqlite_checkpointer(db)
    conn = sqlite3.connect(db)
    for i in range(3):
        _insert(conn, "a2a:chat", _uuid6(i))  # parent roots R0..R2
    for i in range(3, 9):
        _insert(conn, "a2a:chat", _uuid6(i), subagent=True)  # in-flight subagent S3..S8
    conn.commit()
    assert trim_group(conn, "a2a:chat", "", 2) == 1  # only R0 goes
    conn.commit()
    left = [r[0] for r in conn.execute("SELECT checkpoint_id FROM checkpoints ORDER BY checkpoint_id")]
    assert left == [_uuid6(i) for i in range(1, 9)]
    assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 8
    conn.close()
    # The sweep uses the same rule.
    res = prune_checkpoints(db, keep_per_thread=1)
    assert res["checkpoints_deleted"] == 1  # R1; R2 + the subagent rows survive
    assert _rows(db, "checkpoints", "a2a:chat") == 7


# ── deletion + GC ─────────────────────────────────────────────────────────────


def test_chat_delete_takes_the_threads_media_but_not_what_another_chat_shares(tmp_path):
    db = str(tmp_path / "c.db")
    _run_turns(db, 2, thread="a2a:s1")  # images 0, 1
    _run_turns(db, 1, thread="a2a:s2")  # image 0 — shared with s1
    assert _media(db) == 2
    assert delete_thread(db, "a2a:s1", cascade=True) > 0
    assert _media(db) == 1  # image 1 went with s1 immediately; image 0 is still s2's
    saver = build_sqlite_checkpointer(db)
    st = saver.get_tuple({"configurable": {"thread_id": "a2a:s2"}})
    assert st.checkpoint["channel_values"]["messages"][-2].content[1]["image_url"]["url"].endswith(_png(0))


def test_cascade_takes_aside_media_and_spares_a_prefix_sharing_chat(tmp_path):
    db = str(tmp_path / "c.db")
    gone = ("a2a:s1", "a2a:s1::aside-9f", "a2a:s1:goal-iter-2")
    kept = ("a2a:s10", "a2a:s10::aside-9f")
    for seed, tid in enumerate(gone + kept):
        _run_turns(db, 1, seeds=[100 + seed], thread=tid)
    assert _media(db) == 5
    delete_thread(db, "a2a:s1", cascade=True)
    for tid in gone:
        assert (_rows(db, "checkpoints", tid), _rows(db, "writes", tid)) == (0, 0)
    for tid in kept:
        assert _rows(db, "checkpoints", tid) and _rows(db, "writes", tid)
    assert _media(db) == 2  # exactly the two prefix-sharing chats' images survive


def test_saver_delete_thread_takes_media(tmp_path):
    db = str(tmp_path / "c.db")
    saver, _ = _run_turns(db, 2)
    saver.delete_thread("a2a:visual")  # LangGraph's own delete: rows only
    assert _rows(db, "checkpoints", "a2a:visual") == 0
    assert gc_media(db, grace_s=0) == 2  # the sweep's GC frees what nothing references
    assert _media(db) == 0


def test_gc_drops_media_no_row_references(tmp_path):
    db = str(tmp_path / "c.db")
    _run_turns(db, 2)
    conn = sqlite3.connect(db)
    import time

    for h in ("f" * 64, "e" * 64):
        conn.execute("INSERT INTO checkpoint_media (hash, data, created) VALUES (?, x'00', ?)", (h, time.time()))
    conn.commit()
    conn.close()
    assert gc_media(db) == 0  # inside the grace window: a put may be about to reference them
    assert gc_media(db, grace_s=0) == 2
    assert _media(db) == 2  # the two real images stay


# ── reclaim ───────────────────────────────────────────────────────────────────


def test_reclaim_drains_the_whole_freelist(tmp_path):
    """``PRAGMA incremental_vacuum`` frees one page per step of its result: undrained,
    a sweep handed back ONE page (brandLaunch logged ``pages=1`` after deleting 1,988
    checkpoints) and INCREMENTAL DBs sat at 90-99% freelist."""
    db = str(tmp_path / "c.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data BLOB)")
    conn.executemany("INSERT INTO t (data) VALUES (?)", [(os.urandom(4096),) for _ in range(200)])
    conn.commit()
    conn.execute("DELETE FROM t")
    conn.commit()
    freed = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.close()
    assert freed > 100
    res = reclaim(db)
    assert res["pages_reclaimed"] >= freed
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    conn.close()


# ── compaction (the one-time reclaim command) ─────────────────────────────────


def test_plan_is_read_only_and_compact_reclaims(tmp_path):
    db = str(tmp_path / "c.db")
    _run_turns(db, 8, prune_on_write=False, offload_media=False)  # a pre-fix, bloated thread
    before_rows = _rows(db, "checkpoints", "a2a:visual")
    plan = plan_compaction(db, keep_per_thread=2)
    assert _rows(db, "checkpoints", "a2a:visual") == before_rows  # dry run changed nothing
    assert plan["checkpoints_to_delete"] == before_rows - 2
    assert plan["threads"][0]["thread_id"] == "a2a:visual"
    assert plan["bytes_to_delete"] > 0
    res = compact(db, keep_per_thread=2)
    assert res["checkpoints_deleted"] == plan["checkpoints_to_delete"]
    assert _rows(db, "checkpoints", "a2a:visual") == 2
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    conn.close()


def test_compact_cli_is_a_dry_run_by_default(tmp_path, capsys):
    from graph.checkpoint_migrate import run_checkpoint_cli

    db = str(tmp_path / "c.db")
    _run_turns(db, 4, prune_on_write=False)
    n = _rows(db, "checkpoints", "a2a:visual")
    assert run_checkpoint_cli(["compact", "--db", db]) == 0
    assert "dry run" in capsys.readouterr().out
    assert _rows(db, "checkpoints", "a2a:visual") == n
    assert run_checkpoint_cli(["compact", "--db", db, "--apply", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "apply" and out["result"]["checkpoints_deleted"] == n - 2


# ── alerts ────────────────────────────────────────────────────────────────────


class _AlertCfg:
    checkpoint_alert_db_mb = 0
    checkpoint_alert_thread_mb = 0


@pytest.fixture(autouse=True)
def _fresh_stats_cache():
    checkpoint_health._cache.clear()
    yield
    checkpoint_health._cache.clear()


def test_size_warning_names_the_heavy_thread_and_clears(tmp_path, monkeypatch):
    db = str(tmp_path / "c.db")
    _run_turns(db, 3, offload_media=False)
    cfg = _AlertCfg()
    assert checkpoint_health.db_size_warning(db, cfg) is None  # both thresholds off
    monkeypatch.setattr(checkpoint_health, "_MB", 1024)  # thresholds in KB for a tiny DB
    cfg.checkpoint_alert_thread_mb = 1
    warn = checkpoint_health.db_size_warning(db, cfg)
    assert warn and "a2a:visual" in warn and "protoagent checkpoint compact" in warn
    checkpoint_health._cache.clear()
    cfg.checkpoint_alert_thread_mb = 10**6
    assert checkpoint_health.db_size_warning(db, cfg) is None  # self-clearing
    cfg.checkpoint_alert_db_mb = 1
    assert "over the" in checkpoint_health.db_size_warning(db, cfg)


def test_fleet_warning_lists_members_over_threshold_by_stat_only(tmp_path, monkeypatch):
    from graph.workspaces import manager

    big, small = tmp_path / "big", tmp_path / "small"
    for d, n in ((big, 4096), (small, 10)):
        d.mkdir()
        (d / "checkpoints.db").write_bytes(b"\0" * n)
    monkeypatch.setattr(
        manager,
        "list_workspaces",
        lambda: [
            {"id": "big-1", "name": "big", "label": "brandLaunch", "path": str(big)},
            {"id": "small-1", "name": "small", "label": "gymBro", "path": str(small)},
        ],
    )
    monkeypatch.setattr(checkpoint_health, "_MB", 1024)
    cfg = _AlertCfg()
    assert checkpoint_health.fleet_size_warning(cfg) is None
    cfg.checkpoint_alert_db_mb = 2
    warn = checkpoint_health.fleet_size_warning(cfg)
    assert warn and "brandLaunch" in warn and "gymBro" not in warn


def test_runtime_status_banners_the_checkpoint_warning(monkeypatch):
    """The alert reaches the operator through the existing runtime-status warnings[]."""
    from graph import checkpoint_health as ch
    from operator_api import console_handlers
    from runtime.state import STATE

    monkeypatch.setattr(ch, "db_size_warning", lambda path, cfg: "CKPT-BIG")
    monkeypatch.setattr(ch, "fleet_size_warning", lambda cfg: "FLEET-CKPT-BIG")
    monkeypatch.setattr(STATE, "checkpoint_path", "/tmp/whatever.db", raising=False)
    status = asyncio.run(console_handlers._operator_runtime_status())
    assert "CKPT-BIG" in status["warnings"] and "FLEET-CKPT-BIG" in status["warnings"]


# ── #4306 review follow-ups ───────────────────────────────────────────────────


def test_a_real_task_subagent_namespace_is_bounded_on_write(tmp_path):
    """A ``task()``-style subagent (a graph streamed from inside a tool, no checkpointer of
    its own) checkpoints into ``tools:<task_id>`` with ``metadata.parents`` on every row.
    Root-counting there would never trim it (review M1); it is plain newest-N."""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self

    @tool
    def noop() -> str:
        "noop"
        return "ok"

    @tool
    async def task(prompt: str) -> str:
        "delegate"
        steps = [AIMessage(content="", tool_calls=[{"name": "noop", "args": {}, "id": f"c{i}"}]) for i in range(6)]
        child = create_agent(model=_Fake(messages=iter([*steps, AIMessage(content="child done")])), tools=[noop])
        res = {}
        async for st in child.astream({"messages": [{"role": "user", "content": prompt}]}, stream_mode="values"):
            res = st
        return res["messages"][-1].content

    db = str(tmp_path / "c.db")
    saver = build_sqlite_checkpointer(db)
    parent = iter(
        [AIMessage(content="", tool_calls=[{"name": "task", "args": {"prompt": "go"}, "id": "t1"}]), AIMessage(content="done")]
    )
    app = create_agent(model=_Fake(messages=parent), tools=[task], checkpointer=saver)
    cfg = {"configurable": {"thread_id": "a2a:chat-1"}}
    out = asyncio.run(app.ainvoke({"messages": [{"role": "user", "content": "hi"}]}, cfg))
    assert out["messages"][-1].content == "done"
    conn = sqlite3.connect(db)
    per_ns = dict(conn.execute("SELECT checkpoint_ns, COUNT(*) FROM checkpoints GROUP BY checkpoint_ns"))
    conn.close()
    sub = [ns for ns in per_ns if ns]
    assert sub, per_ns  # the subagent really did checkpoint into its own namespace
    assert all(per_ns[ns] <= saver.keep_per_thread for ns in sub), per_ns
    assert per_ns[""] == saver.keep_per_thread
    assert asyncio.run(app.aget_state(cfg)).values["messages"][-1].content == "done"


def test_write_path_trim_never_cuts_a_turn_that_sorts_below_older_ids(tmp_path):
    """Clock went backwards across a restart: the new row's time-ordered id sorts below the
    old ones. The write-path trim must skip, not delete the turn it just wrote (m3)."""
    db = str(tmp_path / "c.db")
    build_sqlite_checkpointer(db)
    conn = sqlite3.connect(db)
    for i in (5, 6, 7):
        _insert(conn, "a2a:t", _uuid6(i))
    _insert(conn, "a2a:t", _uuid6(1))  # just written, but "older"
    conn.commit()
    assert trim_group(conn, "a2a:t", "", 2, written_id=_uuid6(1)) == 0
    assert trim_group(conn, "a2a:t", "", 2, written_id=_uuid6(7)) == 2  # normal case still trims
    conn.close()


def test_a_lone_surrogate_in_a_media_string_does_not_brick_the_thread(tmp_path):
    """m2: hashing/storing must not raise on a string the stock saver can serialize."""
    db = str(tmp_path / "c.db")
    saver = build_sqlite_checkpointer(db)
    weird = "data:image/png;base64," + _png(3) + "\udc80"
    g = StateGraph(MessagesState)
    g.add_node(
        "n",
        lambda s: {"messages": [ToolMessage(content=[{"type": "image_url", "image_url": {"url": weird}}], tool_call_id="x")]},
    )
    g.add_edge(START, "n")
    g.add_edge("n", END)
    app = g.compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": "a2a:s"}}
    asyncio.run(app.ainvoke({"messages": [HumanMessage(content="hi")]}, cfg))
    got = asyncio.run(app.aget_state(cfg)).values["messages"][-1].content[0]["image_url"]["url"]
    assert got == weird


def test_offload_failure_falls_back_to_inline_storage(tmp_path, monkeypatch):
    from graph import checkpoint_serde

    def _boom(*a, **k):
        raise RuntimeError("offload broke")

    monkeypatch.setattr(checkpoint_serde, "_offload_obj", _boom)
    db = str(tmp_path / "c.db")
    _, state = _run_turns(db, 1)
    assert state.values["messages"][-2].content[1]["image_url"]["url"] == "data:image/png;base64," + _png(0)
    assert _media(db) == 0


def test_compact_keeps_a_payload_a_live_agent_just_stored(tmp_path):
    """m1: ``compact --apply`` runs beside a live agent; a payload committed just before its
    row must survive it (a short grace, never zero)."""
    import time

    db = str(tmp_path / "c.db")
    _run_turns(db, 1)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO checkpoint_media (hash, data, created) VALUES (?, x'00', ?)", ("d" * 64, time.time()))
    conn.commit()
    conn.close()
    assert compact(db)["media_deleted"] == 0
    assert _media(db) == 2


def test_a_failed_stats_read_is_not_cached(tmp_path, monkeypatch):
    db = str(tmp_path / "c.db")
    _run_turns(db, 1)
    real = sqlite3.connect

    def _locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", _locked)
    assert checkpoint_health.db_stats(db)["threads"] == []
    monkeypatch.setattr(sqlite3, "connect", real)
    assert checkpoint_health.db_stats(db)["threads"][0][0] == "a2a:visual"  # not the cached failure
