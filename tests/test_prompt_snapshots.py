"""PromptSnapshotStore (#2243) — hash-dedupe, in-write retention, orphan sweep,
call_index assignment, purge, and migration idempotence."""

import sqlite3

from observability.prompt_snapshots import PromptSnapshotStore


def _store(tmp_path, **kw):
    return PromptSnapshotStore(str(tmp_path / "snaps.db"), **kw)


def _count(store, table):
    db = sqlite3.connect(store.path)
    try:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        db.close()


def test_stable_blob_hash_dedupe(tmp_path):
    # The same stable prefix across calls is stored ONCE — that's the whole
    # point of the hash split (the blob is tens of KB, the tail is small).
    s = _store(tmp_path)
    for _ in range(3):
        s.record(task_id="t1", stable_text="BIG STABLE PROMPT", context_text="tail")
    assert _count(s, "calls") == 3
    assert _count(s, "stable_blobs") == 1


def test_call_index_assigned_per_task(tmp_path):
    s = _store(tmp_path)
    s.record(task_id="t1", stable_text="P")
    s.record(task_id="t1", stable_text="P")
    s.record(task_id="t2", stable_text="P")
    assert [c["call_index"] for c in s.calls_for_task("t1")] == [0, 1]
    assert [c["call_index"] for c in s.calls_for_task("t2")] == [0]


def test_call_index_fallback_keys_by_session_and_trace(tmp_path):
    # Non-A2A callers carry no task_id — rows key by (session_id, trace_id)
    # so a turn's calls still index consecutively without cross-talk.
    s = _store(tmp_path)
    s.record(session_id="s1", trace_id="tr1", stable_text="P")
    s.record(session_id="s1", trace_id="tr1", stable_text="P")
    s.record(session_id="s1", trace_id="tr2", stable_text="P")
    last = s.last_for_session("s1")
    assert last["trace_id"] == "tr2"
    assert last["call_index"] == 0


def test_reads_resolve_stable_blob_and_order_by_call(tmp_path):
    s = _store(tmp_path)
    s.record(task_id="t1", stable_text="STABLE", context_text="tail-0", model="m1")
    s.record(task_id="t1", stable_text="STABLE", context_text="tail-1", model="m1")
    calls = s.calls_for_task("t1")
    assert [c["context_text"] for c in calls] == ["tail-0", "tail-1"]
    assert all(c["stable_text"] == "STABLE" for c in calls)
    assert s.calls_for_task("missing") == []
    assert s.last_for_session("nope") is None


def test_usage_round_trips(tmp_path):
    s = _store(tmp_path)
    s.record(
        task_id="t1",
        stable_text="P",
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=80,
        cache_creation_tokens=5,
    )
    row = s.calls_for_task("t1")[0]
    assert (row["input_tokens"], row["output_tokens"]) == (100, 20)
    assert (row["cache_read_tokens"], row["cache_creation_tokens"]) == (80, 5)


def test_count_cap_trims_in_write_and_sweeps_orphan_blobs(tmp_path):
    # max_calls keeps only the newest rows, trimmed inside the write
    # transaction; a stable blob no surviving call references is swept.
    s = _store(tmp_path, retention_days=0, max_calls=2)
    s.record(task_id="t1", stable_text="OLD-PROMPT")
    s.record(task_id="t2", stable_text="NEW-PROMPT")
    s.record(task_id="t3", stable_text="NEW-PROMPT")
    assert _count(s, "calls") == 2
    assert s.calls_for_task("t1") == []  # oldest trimmed
    assert _count(s, "stable_blobs") == 1  # OLD-PROMPT's blob swept


def test_age_cap_trims_old_rows(tmp_path):
    s = _store(tmp_path, retention_days=7, max_calls=0)
    s.record(task_id="old", stable_text="P")
    # Age the row past the cutoff directly — record() stamps now().
    db = sqlite3.connect(s.path)
    db.execute("UPDATE calls SET ts = '2000-01-01T00:00:00+00:00'")
    db.commit()
    db.close()
    s.record(task_id="new", stable_text="P")
    assert s.calls_for_task("old") == []
    assert len(s.calls_for_task("new")) == 1


def test_zero_caps_disable_trimming(tmp_path):
    s = _store(tmp_path, retention_days=0, max_calls=0)
    for i in range(5):
        s.record(task_id=f"t{i}", stable_text="P")
    assert _count(s, "calls") == 5


def test_purge_session_deletes_rows_and_sweeps_blobs(tmp_path):
    # The chat-delete hook: prompts never outlive their conversation.
    s = _store(tmp_path)
    s.record(task_id="t1", session_id="s1", stable_text="ONLY-S1")
    s.record(task_id="t2", session_id="s2", stable_text="ALSO-S2")
    assert s.purge_session("s1") == 1
    assert s.calls_for_task("t1") == []
    assert len(s.calls_for_task("t2")) == 1
    assert _count(s, "stable_blobs") == 1  # s1's blob swept, s2's kept
    assert s.purge_session("") == 0


def test_reopen_is_idempotent(tmp_path):
    # CREATE IF NOT EXISTS schema — constructing over an existing DB neither
    # errors nor drops data (the migration-idempotence contract).
    s = _store(tmp_path)
    s.record(task_id="t1", stable_text="P")
    again = PromptSnapshotStore(s.path)
    assert len(again.calls_for_task("t1")) == 1


# ── #2388 P3: subagent nesting + the previous-turn diff anchor ────────────────


def test_subagent_rows_nest_under_parent_not_task(tmp_path):
    # A subagent call claims NO task_id — it nests under the delegating tool-call
    # id, so the main-loop tabs stay uncontaminated and call_index scopes per
    # (parent, subagent type).
    s = _store(tmp_path)
    s.record(task_id="t1", session_id="s1", stable_text="MAIN")
    s.record(parent_task_id="call-abc", subagent_type="researcher", stable_text="SUB")
    s.record(parent_task_id="call-abc", subagent_type="researcher", stable_text="SUB")
    s.record(parent_task_id="call-abc", subagent_type="verifier", stable_text="SUB2")
    assert len(s.calls_for_task("t1")) == 1  # main turn untouched
    subs = s.calls_for_parent("call-abc")
    assert [(r["subagent_type"], r["call_index"]) for r in subs] == [
        ("researcher", 0),
        ("researcher", 1),
        ("verifier", 0),
    ]
    assert all(r["task_id"] == "" for r in subs)


def test_previous_main_call_anchors_the_turn_diff(tmp_path):
    # The diff anchor: the newest MAIN-LOOP call of the same session strictly
    # older than the current turn's first row. Subagent rows are not turns.
    s = _store(tmp_path)
    s.record(task_id="t1", session_id="s1", stable_text="TURN1")
    s.record(parent_task_id="call-x", subagent_type="researcher", session_id="s1", stable_text="SUB")
    s.record(task_id="t2", session_id="s1", stable_text="TURN2")
    t2 = s.calls_for_task("t2")[0]
    prev = s.previous_main_call("s1", t2["ts"])
    assert prev is not None and prev["task_id"] == "t1"
    # First turn / missing history degrades to None — "no comparison available".
    t1 = s.calls_for_task("t1")[0]
    assert s.previous_main_call("s1", t1["ts"]) is None
    assert s.previous_main_call("", t2["ts"]) is None
