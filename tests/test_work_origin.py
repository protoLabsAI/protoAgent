"""Work provenance resolves through the live stores without changing execution ids."""

from types import SimpleNamespace

import pytest

from background.store import BackgroundStore
from inbox.store import InboxStore
from runtime.state import STATE
from scheduler.local import LocalScheduler
from tools.session import _origin_session_from, origin_session_for


@pytest.fixture
def stores(tmp_path, monkeypatch):
    inbox = InboxStore(str(tmp_path / "inbox.db"))
    scheduler = LocalScheduler("origin-test", invoke_url="http://test", db_dir=tmp_path / "scheduler")
    background = BackgroundStore(str(tmp_path / "background.db"))
    monkeypatch.setattr(STATE, "inbox_store", inbox)
    monkeypatch.setattr(STATE, "scheduler", scheduler)
    monkeypatch.setattr(STATE, "background_mgr", SimpleNamespace(store=background))
    return inbox, scheduler, background


def _inbox_context(inbox, origin):
    item = inbox.add("work", priority="now", origin_session=origin, dedup_key=origin)
    return inbox.execution_identity(item["id"])["context_id"]


def _background_context(background, origin):
    job_id = background.create(agent_name="origin-test", origin_session=origin,
                               subagent_type="test", description="work", prompt="work")
    return f"background:{job_id}"


def test_durable_origins_and_inbox_precedence(stores):
    inbox, scheduler, background = stores
    context = _inbox_context(inbox, "chat-a")
    scheduler.add_job("collision", "0 * * * *", context_id=context, origin_session="chat-other")
    cron = scheduler.add_job("cron", "0 * * * *", origin_session="chat-cron")
    bg = _background_context(background, "thread-a2a")
    for context_id, expected in [(context, "chat-a"), (cron.context_id, "chat-cron"), (bg, "thread-a2a")]:
        assert origin_session_for(context_id) == expected
        assert _origin_session_from({"session_id": context_id}) == expected


def test_chain_bound_and_cycles(stores):
    inbox, scheduler, background = stores
    first = _inbox_context(inbox, "chat-a")
    second = _background_context(background, first)
    third = scheduler.add_job("cron", "0 * * * *", origin_session=second).context_id
    fourth = _background_context(background, third)
    assert origin_session_for(second) == "chat-a"
    assert origin_session_for(third) == "chat-a"
    assert origin_session_for(fourth) == ""
    scheduler.add_job("cycle", "0 * * * *", context_id="job:cycle", origin_session="job:cycle")
    assert origin_session_for("job:cycle") == ""


@pytest.mark.parametrize("context, expected", [
    ("chat-x", "chat-x"), ("plain-thread", "plain-thread"), ("", ""),
    ("job:unknown", ""), ("background:unknown", ""),
])
def test_passthrough_and_unknown(stores, context, expected):
    assert origin_session_for(context) == expected


def test_missing_stores(monkeypatch):
    for field in ("inbox_store", "scheduler", "background_mgr"):
        monkeypatch.setattr(STATE, field, None)
    assert origin_session_for("job:missing") == ""
    assert origin_session_for("background:missing") == ""


@pytest.mark.parametrize("store_index, context", [(0, "job:bad"), (1, "job:bad"), (2, "background:bad")])
def test_store_errors_are_empty(stores, monkeypatch, store_index, context):
    def broken():
        raise OSError("store unavailable")

    monkeypatch.setattr(stores[store_index], "_connect", broken)
    assert origin_session_for(context) == ""
    if store_index == 1:
        assert stores[1].origin_for_context(context) == ""


def test_stale_scheduler_generation_does_not_inherit_replacement_origin(stores):
    _, scheduler, _ = stores
    job = scheduler.add_job("cron", "0 * * * *", origin_session="chat-old")
    db = scheduler._connect()
    try:
        db.execute("UPDATE jobs SET created_at = ?, origin_session = ? WHERE id = ?",
                   ("2099-01-01T00:00:00+00:00", "chat-new", job.id))
        db.commit()
    finally:
        db.close()
    assert origin_session_for(job.context_id) == ""
