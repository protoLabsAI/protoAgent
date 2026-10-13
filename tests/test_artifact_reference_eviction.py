"""Artifact eviction never deletes an answer a surviving chat still shows (#4138, ADR 0118 S20).

Inline answers are artifacts, and inline is the default placement, so the old 20-artifact cap
evicted a busy session's own earlier answers into "no longer available". The store now:

- caps at 200 by default (the ``history`` setting / ``ARTIFACT_HISTORY``);
- stamps the calling CONSOLE chat's session id onto an artifact whenever its ``artifact-ref``
  goes out (create, update, rewrite, save-file);
- over the cap, evicts the oldest artifact no surviving chat shows — a chat is gone only once
  the operator deleted it (core's durable chat tombstone, read via ``graph.sdk``);
- keeps everything (and warns) when every artifact is still shown, and keeps everything when
  the deletion check can't be read (fail safe);
- never evicts a pinned artifact, as before.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "plugins" / "artifact"


def _load(monkeypatch, tmp_path):
    """A fresh package bound to ``tmp_path`` (the same loader the main artifact suite uses)."""
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    for k in [k for k in sys.modules if k.startswith("artifact_under_test")]:
        del sys.modules[k]
    spec = importlib.util.spec_from_file_location(
        "artifact_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["artifact_under_test"] = mod
    spec.loader.exec_module(mod)
    mod._render_status._RENDER_WAIT_MS = 0
    return mod


def _deleted(monkeypatch, art, deleted: set[str] | None):
    """Stand in for core's tombstone read: ``deleted`` is the set of deleted chats (``None`` =
    the check is unavailable). Returns the list of id-sets it was asked about."""
    asked: list[set[str]] = []

    def fake(ids):
        asked.append(set(ids))
        return None if deleted is None else set(ids) & deleted

    monkeypatch.setattr(art._store, "_deleted_chat_sessions", fake)
    return asked


def _show(art, code, session=""):
    state = {"session_id": session} if session else None
    art.show_artifact.invoke({"kind": "html", "code": code, "state": state})
    return art._read_store()["current"]


def _ids(art):
    return [a["id"] for a in art._read_store()["artifacts"]]


def _art(art, aid):
    return art._find(art._read_store(), aid)


# ── the cap ───────────────────────────────────────────────────────────────────────────


def test_default_cap_is_200(monkeypatch, tmp_path):
    monkeypatch.delenv("ARTIFACT_HISTORY", raising=False)
    art = _load(monkeypatch, tmp_path)
    assert art._max_history() == 200


def test_manifest_default_matches_the_code_default():
    import yaml

    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["config"]["history"] == 200
    assert any(f["key"] == "history" for f in manifest["settings"])


def test_cap_setting_is_honoured_from_settings_and_env(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    _deleted(monkeypatch, art, set())
    monkeypatch.setattr(art._config, "_plugin_cfg", lambda: {"history": 4})
    assert art._max_history() == 4
    made = [_show(art, f"<p>{i}</p>") for i in range(7)]
    assert _ids(art) == made[::-1][:4]  # unreferenced → exactly the old oldest-first eviction
    monkeypatch.setenv("ARTIFACT_HISTORY", "2")  # env overrides the UI value
    _show(art, "<p>next</p>")
    assert len(_ids(art)) == 2


# ── stamping ──────────────────────────────────────────────────────────────────────────


def test_create_update_and_rewrite_stamp_the_calling_chat(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    aid = _show(art, "<p>a</p>", "chat-1")
    assert _art(art, aid)["chat_sessions"] == ["chat-1"]
    art.update_artifact.invoke(
        {"old_string": "a", "new_string": "b", "artifact_id": aid, "state": {"session_id": "chat-2"}}
    )
    art.rewrite_artifact.invoke({"code": "<p>c</p>", "artifact_id": aid, "state": {"session_id": "chat-1"}})
    assert _art(art, aid)["chat_sessions"] == ["chat-1", "chat-2"]  # deduped, first-seen order


def test_non_chat_sessions_and_no_session_stamp_nothing(monkeypatch, tmp_path):
    """Only console chats keep a transcript that re-renders the chip; an API / Activity / A2A
    peer context doesn't, and the key stays ABSENT (the pre-#4138 store shape)."""
    art = _load(monkeypatch, tmp_path)
    for sid in ("", "api-123", "activity", "3f2b-uuid-peer"):
        aid = _show(art, f"<p>{sid}</p>", sid)
        assert "chat_sessions" not in _art(art, aid)


def test_no_stamp_when_the_host_cannot_show_the_chip(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art._ref_mod.EMIT = False  # a host without the component seam: no chat ever shows the ref
    aid = _show(art, "<p>x</p>", "chat-1")
    assert "chat_sessions" not in _art(art, aid)


def test_show_service_stamps_the_session_it_is_given(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    r = art.show_service(kind="html", code="<p>x</p>", session_id="chat-9")
    assert r["ok"] and _art(art, r["id"])["chat_sessions"] == ["chat-9"]
    r2 = art.show_service(kind="html", code="<p>y</p>")
    assert "chat_sessions" not in _art(art, r2["id"])


# ── eviction ──────────────────────────────────────────────────────────────────────────


def test_eviction_skips_artifacts_a_live_chat_shows(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "3")
    art = _load(monkeypatch, tmp_path)
    _deleted(monkeypatch, art, set())
    answer = _show(art, "<p>answer</p>", "chat-1")  # the OLDEST artifact
    rest = [_show(art, f"<p>{i}</p>") for i in range(6)]
    ids = _ids(art)
    assert answer in ids and len(ids) == 3
    assert ids == [rest[-1], rest[-2], answer]  # order kept; the unreferenced oldest went first


def test_lookup_only_runs_over_the_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "5")
    art = _load(monkeypatch, tmp_path)
    asked = _deleted(monkeypatch, art, set())
    for i in range(5):
        _show(art, f"<p>{i}</p>", "chat-1")
    assert asked == []  # under the cap: no tombstone read at all
    _show(art, "<p>over</p>", "chat-1")
    assert asked == [{"chat-1"}]


def test_an_artifact_whose_only_chat_was_deleted_becomes_evictable(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "2")
    art = _load(monkeypatch, tmp_path)
    deleted: set[str] = set()
    monkeypatch.setattr(art._store, "_deleted_chat_sessions", lambda ids: set(ids) & deleted)
    old = _show(art, "<p>old answer</p>", "chat-gone")
    a, b = _show(art, "<p>a</p>"), _show(art, "<p>b</p>")
    # chat-gone still exists: the referenced oldest survives, the unreferenced `a` goes.
    assert old in _ids(art) and a not in _ids(art)
    deleted.add("chat-gone")  # the operator deletes the chat
    c = _show(art, "<p>c</p>")
    assert _ids(art) == [c, b]  # now the oldest unreferenced artifact → evicted


def test_a_partly_deleted_artifact_stays_and_its_dead_chats_are_pruned(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "1")
    art = _load(monkeypatch, tmp_path)
    _deleted(monkeypatch, art, {"chat-1"})
    aid = _show(art, "<p>a</p>", "chat-1")
    art.update_artifact.invoke(
        {"old_string": "a", "new_string": "b", "artifact_id": aid, "state": {"session_id": "chat-2"}}
    )
    _show(art, "<p>x</p>")  # over the cap → eviction reads the tombstones
    assert aid in _ids(art)
    assert _art(art, aid)["chat_sessions"] == ["chat-2"]


def test_all_referenced_keeps_everything_and_warns_once(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("ARTIFACT_HISTORY", "2")
    art = _load(monkeypatch, tmp_path)
    _deleted(monkeypatch, art, set())
    caplog.set_level(logging.WARNING, logger="protoagent.plugins.artifact")
    made = [_show(art, f"<p>{i}</p>", f"chat-{i}") for i in range(5)]
    assert _ids(art) == made[::-1]  # nothing deleted — the store sits over its cap
    warnings = [r for r in caplog.records if "over its cap" in r.getMessage()]
    assert len(warnings) == 1  # once per over-cap episode, not once per write
    assert "every remaining artifact is still shown" in warnings[0].getMessage()
    # …and the agent-facing listing says so too.
    assert "The store is 3 over that cap" in art.list_artifacts.invoke({})


def test_over_cap_evicts_only_down_to_the_cap(monkeypatch, tmp_path):
    """Mixed: the store is over by 3 with only 2 unreferenced → both go, it stays 1 over."""
    monkeypatch.setenv("ARTIFACT_HISTORY", "100")
    art = _load(monkeypatch, tmp_path)
    _deleted(monkeypatch, art, set())
    refs = [_show(art, f"<p>r{i}</p>", "chat-1") for i in range(3)]
    loose = [_show(art, f"<p>l{i}</p>") for i in range(2)]
    monkeypatch.setenv("ARTIFACT_HISTORY", "2")
    _show(art, "<p>last</p>", "chat-1")
    ids = _ids(art)
    assert set(refs) <= set(ids) and not set(loose) & set(ids) and len(ids) == 4


def test_unreadable_deletions_fail_safe_keep_every_stamped_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "1")
    art = _load(monkeypatch, tmp_path)
    _deleted(monkeypatch, art, None)  # the check is unavailable
    stamped = _show(art, "<p>s</p>", "chat-1")
    loose = _show(art, "<p>l</p>")
    newest = _show(art, "<p>n</p>")  # the artifact just written is never the one evicted
    ids = _ids(art)
    assert stamped in ids and loose not in ids and newest in ids
    assert _art(art, stamped)["chat_sessions"] == ["chat-1"]  # nothing pruned on a guess


def test_pinned_artifacts_are_never_evicted(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "2")
    art = _load(monkeypatch, tmp_path)
    _deleted(monkeypatch, art, {"chat-gone"})
    pinned = _show(art, "<p>pinned</p>", "chat-gone")  # pinned AND its only chat is deleted
    assert "Pinned artifact" in art.pin_artifact.invoke({"artifact_id": pinned})
    for i in range(6):
        _show(art, f"<p>{i}</p>")
    ids = _ids(art)
    assert ids[0] == pinned and len(ids) == 3  # the pin + the 2 newest, as before


# ── the core read: graph.sdk.deleted_chat_sessions over the real tombstone table ────────


@pytest.fixture
def task_db(monkeypatch, tmp_path):
    """Point ``instance_paths().store()`` at a tmp dir; returns the a2a-tasks.db path."""
    import infra.paths as paths

    class _P:
        def store(self, name):
            return tmp_path / name

    monkeypatch.setattr(paths, "instance_paths", lambda: _P())
    return tmp_path / "a2a-tasks.db"


def _retire(db: Path, *session_ids: str) -> None:
    """Delete chats the way the DELETE route does: core's own tombstone writer."""
    from sqlalchemy.ext.asyncio import create_async_engine

    import operator_api.chat_routes as cr

    async def _go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        try:
            async with engine.begin() as conn:
                await cr._ensure_chat_tombstones(conn)
                for sid in session_ids:
                    await cr._record_chat_tombstone(conn, sid)
        finally:
            await engine.dispose()

    asyncio.run(_go())


def test_sdk_reads_core_tombstones(task_db):
    from graph import sdk

    _retire(task_db, "chat-a", "chat-b")
    assert sdk.deleted_chat_sessions({"chat-a", "chat-c"}) == {"chat-a"}
    assert sdk.deleted_chat_sessions([]) == set()


def test_sdk_unknown_without_a_task_db_and_empty_without_the_table(task_db):
    import sqlite3

    from graph import sdk

    assert sdk.deleted_chat_sessions({"chat-a"}) is None  # no DB → unknown (and not created)
    assert not task_db.exists()
    sqlite3.connect(str(task_db)).close()  # a task DB where no chat was ever deleted
    assert sdk.deleted_chat_sessions({"chat-a"}) == set()


def test_end_to_end_a_deleted_chat_releases_its_artifact(monkeypatch, tmp_path, task_db):
    """No fake at the plugin seam: the store reads core's real tombstone table via graph.sdk."""
    monkeypatch.setenv("ARTIFACT_HISTORY", "1")
    art = _load(monkeypatch, tmp_path)
    _retire(task_db)  # the table exists, nothing deleted yet
    answer = _show(art, "<p>answer</p>", "chat-live")
    _show(art, "<p>x</p>")
    assert answer in _ids(art)
    _retire(task_db, "chat-live")
    newest = _show(art, "<p>y</p>")
    assert _ids(art) == [newest]
