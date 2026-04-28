"""Tests for ``scheduler.workstacean.WorkstaceanScheduler``.

We don't run a Workstacean instance — instead we monkeypatch
``httpx.post`` and assert that the adapter sends the right
``POST /publish`` body shape (action, namespaced id, namespaced topic,
auth header).
"""

from __future__ import annotations

from typing import Any

import pytest

from scheduler.workstacean import WorkstaceanScheduler


class _FakeResponse:
    def __init__(self, status: int = 200, body: str = "ok"):
        self.status_code = status
        self.text = body


class _Recorder:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.response = _FakeResponse()

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    import httpx
    monkeypatch.setattr(httpx, "post", rec)
    return rec


@pytest.fixture
def adapter():
    return WorkstaceanScheduler(
        agent_name="gina-personal",
        base_url="http://workstacean:3000",
        api_key="test-key",
    )


# ── construction guards ────────────────────────────────────────────────────


def test_missing_base_url_rejected():
    with pytest.raises(ValueError, match="base_url"):
        WorkstaceanScheduler(agent_name="x", base_url="", api_key="k")


def test_missing_api_key_rejected():
    with pytest.raises(ValueError, match="api_key"):
        WorkstaceanScheduler(agent_name="x", base_url="http://w:3000", api_key="")


# ── add_job ────────────────────────────────────────────────────────────────


class TestAddJob:
    def test_publishes_command_schedule(self, adapter, recorder):
        adapter.add_job("hi", "0 9 * * *", job_id="daily")
        assert len(recorder.calls) == 1
        body = recorder.calls[0]["json"]
        assert body["topic"] == "command.schedule"
        assert body["payload"]["action"] == "add"

    def test_id_namespaced_with_agent(self, adapter, recorder):
        adapter.add_job("hi", "0 9 * * *", job_id="daily")
        body = recorder.calls[0]["json"]
        assert body["payload"]["id"] == "gina-personal-daily"

    def test_id_idempotent_when_already_prefixed(self, adapter, recorder):
        # If the caller passes an already-prefixed id, the adapter
        # shouldn't double-prefix it.
        adapter.add_job("hi", "0 9 * * *", job_id="gina-personal-already-set")
        body = recorder.calls[0]["json"]
        assert body["payload"]["id"] == "gina-personal-already-set"

    def test_topic_namespaced_with_agent(self, adapter, recorder):
        adapter.add_job("hi", "0 9 * * *", job_id="daily")
        body = recorder.calls[0]["json"]
        assert body["payload"]["topic"].startswith("cron.gina-personal.")

    def test_inner_payload_carries_prompt(self, adapter, recorder):
        adapter.add_job("the actual prompt", "0 9 * * *", job_id="x")
        inner = recorder.calls[0]["json"]["payload"]["payload"]
        assert inner["content"] == "the actual prompt"
        assert inner["channel"] == "a2a"
        assert inner["agent_name"] == "gina-personal"

    def test_iso_oneshot_accepted(self, adapter, recorder):
        adapter.add_job("hi", "2099-01-01T00:00:00", job_id="x")
        assert len(recorder.calls) == 1

    def test_malformed_schedule_rejected(self, adapter):
        with pytest.raises(ValueError, match="Invalid isoformat|could not convert"):
            adapter.add_job("hi", "not-a-schedule", job_id="x")

    def test_empty_prompt_rejected(self, adapter):
        with pytest.raises(ValueError, match="prompt"):
            adapter.add_job("   ", "0 9 * * *", job_id="x")

    def test_auth_header_sent(self, adapter, recorder):
        adapter.add_job("hi", "0 9 * * *", job_id="x")
        assert recorder.calls[0]["headers"]["X-API-Key"] == "test-key"


# ── cancel_job ─────────────────────────────────────────────────────────────


class TestCancelJob:
    def test_publishes_remove(self, adapter, recorder):
        adapter.cancel_job("daily")
        body = recorder.calls[0]["json"]
        assert body["payload"]["action"] == "remove"
        assert body["payload"]["id"] == "gina-personal-daily"

    def test_returns_true_on_success(self, adapter, recorder):
        assert adapter.cancel_job("daily") is True

    def test_returns_false_on_http_error(self, adapter, recorder):
        recorder.response = _FakeResponse(status=500, body="boom")
        assert adapter.cancel_job("daily") is False


# ── topic prefix override ──────────────────────────────────────────────────


def test_custom_topic_prefix(monkeypatch):
    rec = _Recorder()
    import httpx
    monkeypatch.setattr(httpx, "post", rec)
    adapter = WorkstaceanScheduler(
        agent_name="gina-personal",
        base_url="http://w:3000",
        api_key="k",
        topic_prefix="myorg.bus.gina",
    )
    adapter.add_job("hi", "0 9 * * *", job_id="x")
    body = rec.calls[0]["json"]
    assert body["payload"]["topic"].startswith("myorg.bus.gina.")


# ── list_jobs is intentionally empty ───────────────────────────────────────


def test_list_jobs_returns_empty(adapter):
    """Workstacean's ``list`` action publishes async to a topic;
    the adapter doesn't subscribe, so list_jobs returns []."""
    assert adapter.list_jobs() == []


# ── start/stop are no-ops ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_stop_no_op(adapter):
    # Should not raise
    await adapter.start()
    await adapter.stop()
