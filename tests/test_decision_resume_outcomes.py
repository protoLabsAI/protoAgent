"""Each branch of the loopback resume reports a distinct outcome (#5099 review)."""
import logging

import httpx
import pytest

from graph import decision_resume
from graph.decision_resume import ResumeOutcome, resume_parked_turn
from runtime.state import STATE


def _task(state, text="done"):
    return {"jsonrpc": "2.0", "id": "1", "result": {"task": {"id": "t", "status": {
        "state": state, "message": {"role": "ROLE_AGENT", "parts": [{"text": text}]}}}}}


@pytest.fixture
def post(monkeypatch):
    monkeypatch.setattr(STATE, "active_port", 7871)
    seen = {}

    def install(reply):
        async def fake(self, url, **kwargs):
            seen["timeout"] = self.timeout
            if isinstance(reply, BaseException):
                raise reply
            return reply

        monkeypatch.setattr(httpx.AsyncClient, "post", fake)
        return seen

    return install


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, outcome, level, words", [
    (httpx.Response(200, json=_task("TASK_STATE_COMPLETED")), ResumeOutcome.DELIVERED, None, None),
    (httpx.Response(200, json=_task("TASK_STATE_INPUT_REQUIRED")), ResumeOutcome.DELIVERED, None, None),
    (httpx.Response(503, text="busy"), ResumeOutcome.NOT_DELIVERED, logging.WARNING, "HTTP 503"),
    (httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "error": {"code": -32602, "message": "stale"}}),
     ResumeOutcome.NOT_DELIVERED, logging.WARNING, "refused"),
    (httpx.Response(200, json=_task("TASK_STATE_FAILED", "gateway went away")),
     ResumeOutcome.TURN_FAILED, logging.WARNING, "gateway went away"),
    (httpx.Response(200, json={"result": {"id": "t", "status": {"state": "failed"}}}),  # v0.3 shape
     ResumeOutcome.TURN_FAILED, logging.WARNING, "ended failed"),
    (httpx.ReadTimeout("slow"), ResumeOutcome.UNKNOWN, logging.WARNING, "may still be running"),
    (httpx.ConnectError("offline"), ResumeOutcome.NOT_DELIVERED, logging.ERROR, "resume POST failed"),
])
async def test_each_branch_has_its_own_outcome(post, caplog, reply, outcome, level, words):
    post(reply)
    caplog.set_level(logging.INFO, logger="protoagent.decisions")
    result = await resume_parked_turn("s", "yes", "dec-1")
    assert result is outcome
    assert bool(result) is (outcome in (ResumeOutcome.DELIVERED, ResumeOutcome.TURN_FAILED))
    if words:
        hit = [r for r in caplog.records if words in r.getMessage() or words in (r.exc_text or "")]
        assert hit and hit[0].levelno == level, caplog.text


@pytest.mark.asyncio
async def test_no_port_is_not_delivered(monkeypatch):
    monkeypatch.setattr(STATE, "active_port", None)
    assert await resume_parked_turn("s", "yes", "dec-1") is ResumeOutcome.NOT_DELIVERED


@pytest.mark.asyncio
async def test_only_an_operator_answer_drops_the_read_deadline(post):
    seen = post(httpx.Response(200, json=_task("TASK_STATE_COMPLETED")))
    await resume_parked_turn("s", "yes", "dec-1", resolution={"state": "answered", "answer": "yes"})
    assert seen["timeout"].read is None
    for kwargs in ({}, {"resolution": {"state": "expired", "answer": "yes"}}):
        await resume_parked_turn("s", "yes", "dec-1", **kwargs)
        assert seen["timeout"].read == decision_resume.RESUME_TIMEOUT_S
    await resume_parked_turn("s", decision_resume.CANCELLED_RESUME)
    assert seen["timeout"].read == decision_resume.RESUME_TIMEOUT_S
