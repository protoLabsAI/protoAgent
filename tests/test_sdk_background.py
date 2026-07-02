"""graph.sdk.spawn_background / background_status — the plugin↔background channel
(ADR 0043 consumption SDK over ADR 0050 spawn + the ADR 0070 results pipeline)."""

from __future__ import annotations

import pytest

from background.store import BackgroundStore


class _FakeManager:
    """Captures spawn kwargs and exposes a REAL jobs store, so status reads exercise
    the actual row shape (created_at/completed_at/result) rather than a stub's."""

    def __init__(self, store: BackgroundStore):
        self.store = store
        self.spawn_calls: list[dict] = []

    async def spawn(self, **kw) -> str:
        self.spawn_calls.append(kw)
        return self.store.create(
            agent_name="a",
            origin_session=kw["origin_session"],
            subagent_type=kw["subagent_type"],
            description=kw["description"],
            prompt=kw["prompt"],
        )


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    from graph import sdk

    m = _FakeManager(BackgroundStore(str(tmp_path / "jobs.db")))
    monkeypatch.setattr(sdk.STATE, "background_mgr", m, raising=False)
    return m


# ── spawn_background ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_background_round_trip(mgr):
    from graph import sdk

    out = await sdk.spawn_background(
        "Chart the frontier and report discoveries.",
        subagent_type="researcher",
        origin_session="chat-1",
        label="Frontier campaign",
    )
    assert out["ok"] is True
    assert out["task_id"].startswith("bg-")
    assert "chat-1" in out["message"]
    assert mgr.spawn_calls == [
        {
            "origin_session": "chat-1",
            "subagent_type": "researcher",
            "description": "Frontier campaign",
            "prompt": "Chart the frontier and report discoveries.",
        }
    ]
    # The job landed in the durable store as a running row for the origin session.
    job = mgr.store.get(out["task_id"])
    assert job is not None and job.status == "running" and job.origin_session == "chat-1"


@pytest.mark.asyncio
async def test_spawn_background_label_defaults_to_first_prompt_line(mgr):
    from graph import sdk

    out = await sdk.spawn_background(
        "Survey every uncharted system.\nThen report back.",
        subagent_type="researcher",
        origin_session="chat-1",
    )
    assert out["ok"] is True
    assert mgr.spawn_calls[0]["description"] == "Survey every uncharted system."


@pytest.mark.asyncio
async def test_spawn_background_rejects_bad_inputs(mgr):
    from graph import sdk

    out = await sdk.spawn_background("", subagent_type="researcher", origin_session="chat-1")
    assert out["ok"] is False and "prompt" in out["message"]

    out = await sdk.spawn_background("go", subagent_type="researcher", origin_session=" ")
    assert out["ok"] is False and "origin_session" in out["message"]

    out = await sdk.spawn_background("go", subagent_type="no-such-role", origin_session="chat-1")
    assert out["ok"] is False and "no-such-role" in out["message"]
    assert mgr.spawn_calls == []  # nothing fired


@pytest.mark.asyncio
async def test_spawn_background_degrades_without_a_manager(monkeypatch):
    from graph import sdk

    monkeypatch.setattr(sdk.STATE, "background_mgr", None, raising=False)
    out = await sdk.spawn_background("go", subagent_type="researcher", origin_session="chat-1")
    assert out["ok"] is False and "unavailable" in out["message"]


# ── background_status ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_background_status_running_then_finished(mgr):
    from graph import sdk

    spawned = await sdk.spawn_background(
        "Chart the frontier.", subagent_type="researcher", origin_session="chat-1"
    )
    task_id = spawned["task_id"]

    running = sdk.background_status(task_id)
    assert running["ok"] is True
    assert running["status"] == "running"
    assert running["description"] == "Chart the frontier."
    assert running["subagent_type"] == "researcher"
    assert "report" not in running  # no report until the job is terminal

    mgr.store.mark_complete(task_id, "completed", "Charted 12 systems.")
    done = sdk.background_status(task_id)
    assert done["status"] == "completed"
    assert done["report"] == "Charted 12 systems."
    assert done["completed_at"]


def test_background_status_failed_carries_the_report(mgr):
    from graph import sdk

    task_id = mgr.store.create(
        agent_name="a", origin_session="chat-1", subagent_type="researcher", description="d", prompt="p"
    )
    mgr.store.mark_complete(task_id, "failed", "Gateway timed out.")
    out = sdk.background_status(task_id)
    assert out["ok"] is True and out["status"] == "failed" and out["report"] == "Gateway timed out."


def test_background_status_unknown_id(mgr):
    from graph import sdk

    out = sdk.background_status("bg-nope")
    assert out == {"ok": False, "status": "unknown", "message": "no background job 'bg-nope'"}


def test_background_status_degrades_without_a_manager(monkeypatch):
    from graph import sdk

    monkeypatch.setattr(sdk.STATE, "background_mgr", None, raising=False)
    out = sdk.background_status("bg-x")
    assert out["ok"] is False and out["status"] == "unknown" and "unavailable" in out["message"]
