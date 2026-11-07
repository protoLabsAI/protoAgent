"""In-flight duplicate guard for background ``delegate_to`` (protoAgent#4832).

A background delegation to any delegate (not just a managed-git coder) used to be
sendable twice while the first was still running, so both runs applied side effects
(friction 4512aeca: two brandLaunch runs produced conflicting post / queue / brand-kit
state). These tests pin the new behaviour: a second background delegation with the same
target and the same (whitespace-/case-normalised) query is refused while the first is
running — naming the running job id — unless ``force=True``; and once the first job
settles (or for a different target / query / a foreground call / a resume) the send goes
through unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import plugins.delegates as P
import runtime.state as rs
from plugins.delegates.registry import DelegateRegistry

_OPUS = {"name": "opus", "type": "openai", "url": "https://g/v1", "model": "m"}
_SONNET = {"name": "sonnet", "type": "openai", "url": "https://g/v1", "model": "m"}
_CODER = {"name": "coder", "type": "acp", "command": "proto", "workdir": "/tmp"}


# ── fakes ──────────────────────────────────────────────────────────────────────


class _Job:
    """A background-store row — only the fields the dedup liveness check reads."""

    def __init__(self, job_id: str, status: str = "running", created_at: str | None = None):
        self.id = job_id
        self.status = status
        self.created_at = created_at or datetime.now(UTC).isoformat()


class _Store:
    """Just enough of ``background.store.BackgroundStore`` for liveness: ``get(job_id)``."""

    def __init__(self):
        self.jobs: dict[str, _Job] = {}

    def get(self, job_id: str):
        return self.jobs.get(job_id)


class _BgManager:
    """A background manager that records each ``spawn_work`` and registers a ``running``
    store row, WITHOUT running the work — so liveness is exactly what the store row says,
    which a test flips directly. This mirrors the real manager's shape: liveness is read
    from ``self.store``, the authoritative registry, not from a parallel flag."""

    def __init__(self):
        self.store = _Store()
        self.calls: list[dict] = []
        self._n = 0

    async def spawn_work(self, *, work, **kwargs):
        self._n += 1
        job_id = f"bg-{self._n:04d}"
        self.calls.append({"work": work, **kwargs})
        self.store.jobs[job_id] = _Job(job_id, status="running")
        return job_id


# ── harness ──────────────────────────────────────────────────────────────────


def _register(delegates, monkeypatch):
    monkeypatch.setattr(P, "_load_delegates_config", lambda: delegates)
    from graph.plugins.host import PluginHost

    class _Reg:
        def __init__(self):
            self.config = {}
            self.tools = []
            self.host = PluginHost()

        def register_tool(self, t):
            self.tools.append(t)

    r = _Reg()
    P.register(r)
    return r


async def _invoke(tool, args):
    """Invoke through the ToolNode-shaped call so InjectedState/ToolCallId resolve."""
    result = await tool.ainvoke({"name": "delegate_to", "args": args, "id": "test-call", "type": "tool_call"})
    return getattr(result, "content", result)


@pytest.fixture(autouse=True)
def _clear_inflight():
    """The dedup index is module-level process state — isolate each test."""
    P._INFLIGHT_DELEGATIONS.clear()
    yield
    P._INFLIGHT_DELEGATIONS.clear()


@pytest.fixture
def mgr(monkeypatch):
    m = _BgManager()
    monkeypatch.setattr(rs.STATE, "background_mgr", m, raising=False)
    return m


# ── r1: a live duplicate is refused and names the running job ──────────────────


async def test_duplicate_background_delegation_is_refused_and_names_the_job(mgr, monkeypatch):
    tool = _register([_OPUS], monkeypatch).tools[0]

    first = await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    assert "bg-0001" in first

    # Same ask, only noisier whitespace + different case — the normalised key still matches.
    dup = await _invoke(tool, {"target": "opus", "query": "  LAUNCH   the\n brand ", "background": True})
    assert "Refused" in dup
    assert "bg-0001" in dup  # the refusal names the running job id
    assert "force=true" in dup
    assert len(mgr.calls) == 1  # the duplicate did NOT spawn a second job


# ── r2: force=true sends anyway ────────────────────────────────────────────────


async def test_force_sends_a_second_run_anyway(mgr, monkeypatch):
    tool = _register([_OPUS], monkeypatch).tools[0]

    first = await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    assert "bg-0001" in first

    forced = await _invoke(
        tool, {"target": "opus", "query": "launch the brand", "background": True, "force": True}
    )
    assert "Refused" not in forced
    assert "bg-0002" in forced
    assert len(mgr.calls) == 2


# ── r3: once the first job settles, the same send is accepted ──────────────────


@pytest.mark.parametrize("terminal", ["completed", "failed", "canceled"])
async def test_resend_accepted_after_first_job_settles(mgr, monkeypatch, terminal):
    tool = _register([_OPUS], monkeypatch).tools[0]

    first = await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    assert "bg-0001" in first

    # The first job reaches a terminal state in the store (the authoritative registry) —
    # finished, failed or stopped all free the key.
    mgr.store.jobs["bg-0001"].status = terminal

    again = await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    assert "Refused" not in again
    assert "bg-0002" in again
    assert len(mgr.calls) == 2


async def test_resend_accepted_after_first_job_pruned(mgr, monkeypatch):
    """A job that vanished from the store (pruned/unknown) is treated as not-live."""
    tool = _register([_OPUS], monkeypatch).tools[0]

    await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    mgr.store.jobs.pop("bg-0001")

    again = await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    assert "Refused" not in again
    assert "bg-0002" in again


# ── r4: different target / query / foreground / resume are never refused ───────


async def test_different_target_is_not_refused(mgr, monkeypatch):
    tool = _register([_OPUS, _SONNET], monkeypatch).tools[0]

    await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    other = await _invoke(tool, {"target": "sonnet", "query": "launch the brand", "background": True})
    assert "Refused" not in other
    assert len(mgr.calls) == 2


async def test_different_query_is_not_refused(mgr, monkeypatch):
    tool = _register([_OPUS], monkeypatch).tools[0]

    await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    other = await _invoke(tool, {"target": "opus", "query": "retire the brand", "background": True})
    assert "Refused" not in other
    assert len(mgr.calls) == 2


async def test_foreground_calls_are_never_refused(mgr, monkeypatch):
    tool = _register([_OPUS], monkeypatch).tools[0]

    async def _dispatch(self, name, query, **kwargs):
        return f"fg:{query}"

    monkeypatch.setattr(DelegateRegistry, "dispatch", _dispatch)

    a = await _invoke(tool, {"target": "opus", "query": "launch the brand"})
    b = await _invoke(tool, {"target": "opus", "query": "launch the brand"})
    assert a == b == "fg:launch the brand"
    assert mgr.calls == []  # a foreground consult never spawns a background job


async def test_resume_task_id_is_never_refused(mgr, monkeypatch):
    """A resume is a continuation of a parked task, not a fresh send — exempt from dedup."""
    tool = _register([_OPUS], monkeypatch).tools[0]

    first = await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    assert "bg-0001" in first

    resumed = await _invoke(
        tool,
        {"target": "opus", "query": "launch the brand", "background": True, "resume_task_id": "task-42"},
    )
    assert "Refused" not in resumed
    assert "bg-0002" in resumed
    assert len(mgr.calls) == 2


async def test_item_id_coder_path_is_untouched(mgr, monkeypatch):
    """A managed-git coding job keeps its own claim-based dedup in registry.dispatch; the
    new guard must not add a second refusal on the item_id path."""
    tool = _register([_CODER], monkeypatch).tools[0]

    first = await _invoke(
        tool, {"target": "coder", "query": "ship the feature", "background": True, "item_id": "card-7"}
    )
    assert "bg-0001" in first

    again = await _invoke(
        tool, {"target": "coder", "query": "ship the feature", "background": True, "item_id": "card-7"}
    )
    assert "Refused" not in again
    assert len(mgr.calls) == 2


# ── degraded path: no store ⇒ fall open (never refuse on an unverifiable signal) ─


async def test_no_store_manager_never_refuses(monkeypatch):
    """A manager without a store (lean/CLI contexts) can't confirm liveness, so a re-send
    is allowed rather than blocked on a signal that can't be read."""

    class _NoStore:
        def __init__(self):
            self.calls = 0

        async def spawn_work(self, *, work, **kwargs):
            self.calls += 1
            return f"job-{self.calls}"

    m = _NoStore()
    monkeypatch.setattr(rs.STATE, "background_mgr", m, raising=False)
    tool = _register([_OPUS], monkeypatch).tools[0]

    await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    again = await _invoke(tool, {"target": "opus", "query": "launch the brand", "background": True})
    assert "Refused" not in again
    assert m.calls == 2
