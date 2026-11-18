"""K2 derived-reader proofs for callers resolved through production authentication.

Steering scenarios moved to P1h.3 (bd-o4z6e): there is no caller-to-queue path yet.
Reused reader scenarios preserve their assertions and fixture cleanup; digest delivery differs
for federation, whose prior-session gate stays enabled per ADR 0066.

Two groups, counted differently by the Phase 1 gate (#5482):

* Per-caller proofs, parametrized over ``CALLER_KINDS``: prompt capture (the real
  ``/api/prompts/*`` routes with each caller's token) and digest
  (``knowledge._restrict_prior_session_memory()`` reads the bound principal).
* Caller-independent reader proofs, run ONCE: session gist, tracing, trace export and
  harvest. These readers take no caller: product code never reads the verified principal or
  the approval authority on those paths, so a per-kind run would repeat one caller-blind
  proof four times and prove nothing extra. They show the reader keeps private content out
  for everyone; they are not evidence about any particular caller kind.
"""

import inspect
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import SystemMessage
from starlette.testclient import TestClient

from a2a_impl import auth
from security import devices
from tests.test_conversation_harvest import (
    _ARCHIVE_CANARY,
    test_harvest_excludes_private_canaries_from_every_derived_reader as _src_harvest,
)
from tests.test_prompt_capture_middleware import (
    _Req, _response, _run_chained,
    test_private_partition_captures_nothing_and_drops_the_private_buffers as _src_prompt,
)
from tests.test_session_gist import (
    harness as harness,
    test_private_source_survives_public_turn_compaction_restart_and_stale_timer as _src_gist,
)
from tests.test_trace_export import (
    _reset as _reset,
    test_later_public_terminal_export_uses_only_public_bytes as _src_export,
)
from tests.test_tracing import (
    _restore_tracing_module as _restore_tracing_module,
    session_langfuse as session_langfuse,
    test_a_private_partition_records_no_content_now_or_on_a_later_public_turn as _src_tracing,
)

# Device-registry credential minting is platform sensitive.
pytestmark = pytest.mark.platform_sensitive
CALLER_KINDS = [pytest.param((case, kind, operator), id=label) for case, kind, operator, label in (
    ("federation", "federation", False, "federation"), ("fleet", "fleet-service", False, "fleet-service"),
    ("agent", "agent-device", False, "paired-agent"), ("device", "device", True, "enrolled-console"),
)]

@contextmanager
def _caller_scope(caller):
    from graph.middleware.request_context import approval_authority_scope, verified_principal_scope
    with verified_principal_scope(caller.ctx.state["principal_kind"]), approval_authority_scope(caller.ctx.state["trust_tier"] != "federation"):
        yield

def _resolve(case):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from a2a_impl.stores import tier_context_builder
    token = {"federation": "fed"}.get(case, case)
    device_id = ""
    if case in ("device", "agent"):
        code, _ = devices.start_pairing(kind=case)
        device, token = devices.claim_pairing(code, f"proof-{case}")
        device_id = str(device["id"])
    holder: dict = {}
    def inspect(request):
        holder["ctx"] = tier_context_builder().build(request)
        return JSONResponse(holder["ctx"].state["principal_kind"])
    app = Starlette(routes=[Route("/a2a", inspect)])
    app.add_middleware(auth.A2AAuthMiddleware)
    with TestClient(app) as client:
        resp = client.get("/a2a", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return holder["ctx"], device_id, token

@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "_registry_path", lambda: tmp_path / "caller-devices.json")
    for name in ("_BEARER", "_FEDERATION", "_FLEET", "_API_KEY", "_ALLOWED_ORIGINS"):
        monkeypatch.setattr(auth, name, list(getattr(auth, name)))
    auth.configure(bearer_token="shared", federation_token="fed", fleet_token="fleet", api_key="legacy", allowed_origins_raw="")

@pytest.fixture(params=CALLER_KINDS)
def caller(request, auth_env):
    case, expected_kind, is_operator = request.param
    ctx, device_id, token = _resolve(case)
    assert ctx.state["principal_kind"] == expected_kind and auth.is_human_operator(ctx.state["principal_kind"]) is is_operator
    principal = f"{expected_kind}:{device_id}" if device_id else expected_kind
    return SimpleNamespace(ctx=ctx, principal=principal, is_operator=is_operator, token=token)

# Caller-independent reader proofs: no caller is bound, because these readers take none.

def test_caller_independent_reader_keeps_private_out_gist(harness, tmp_path, monkeypatch):
    _src_gist(harness, tmp_path, monkeypatch)

@pytest.mark.parametrize("provenance", [None, {"audience": "mixed", "version": "1"}, {"audience": "public", "version": "old"}])
def test_public_projection_filters_prompts_per_caller_kind(caller, tmp_path, monkeypatch, provenance):
    from graph.middleware import knowledge, memory
    with _caller_scope(caller):
        is_federation = caller.ctx.state["principal_kind"] == "federation"
        assert knowledge._restrict_prior_session_memory() is is_federation
        from datetime import datetime, timezone
        from langchain_core.messages import AIMessage, HumanMessage
        from graph import public_memory
        from graph.session_search import search_session_summaries
        from tests._public_memory_source import PublicMemorySource
        monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
        monkeypatch.setattr(memory, "_PERSISTENCE_DISABLED", False)
        _quiet_working_state(monkeypatch)
        _write(tmp_path, "chat-1", "PRIVATE-CANARY", gist="PRIVATE-GIST")
        path = tmp_path / "chat-1.json"
        old = json.loads(path.read_text())
        if provenance is not None:
            old[public_memory.PROVENANCE] = provenance
        path.write_text(json.dumps(old))
        mw = knowledge.KnowledgeMiddleware(None)
        mw.compose_context({"session_id": "peer", "messages": [HumanMessage(content="hello")]}, record=False)
        search_session_summaries("PRIVATE", memory_dir=str(tmp_path))  # prime the FTS cache
        source = PublicMemorySource(tmp_path / "source.record")
        source.save([HumanMessage(content="rocket launch"), AIMessage(content="ready")])
        monkeypatch.setattr(public_memory, "_source_reader", source)
        closed = tmp_path / "closed"
        closed.mkdir()
        now = datetime.now(timezone.utc).isoformat()
        (closed / path.name).write_text(json.dumps({**old, "closed_at": now}))
        assert not memory.record_closed_session("chat-1", topic="PRIVATE", gist="PRIVATE", closed_at=now)
        assert search_session_summaries("PRIVATE", memory_dir=str(tmp_path)) == []
        assert memory.digest_entry(old)["topic"] == ""
        assert memory.format_session_summary(old) == ""
        assert not memory.record_closed_session("chat-1", topic="PRIVATE", gist="PRIVATE", closed_at=now, source_card=old,
                                                audience="public", source_version=1)
        for policy in ("newest", "relevant"):
            assert memory.load_digest(policy, query="PRIVATE", memory_dir=str(tmp_path)).entries == []
        assert "PRIVATE" not in (mw.compose_context({"session_id": "peer", "messages": []}, record=False) or {}).get("context", "")
        memory._persist_session({"session_id": "chat-1", "incognito": False,
                                 "messages": [HumanMessage(content="PRIVATE-COMPACTED"), AIMessage(content="PRIVATE-REPLY")]}, "trace")
        card = json.loads(path.read_text())
        assert "PRIVATE" not in json.dumps(card)
        for policy in ("newest", "relevant"):
            assert "rocket launch" in memory.load_digest(policy, query="rocket", memory_dir=str(tmp_path)).block
        card.update(audience="public", source_version=1)
        assert memory.record_closed_session("chat-1", topic="ignored", gist="ignored", closed_at=now, source_card=card,
                                            audience="mixed", source_version=0)
        closed_card = json.loads((closed / path.name).read_text())
        assert closed_card["audience"] == "public" and closed_card["source_version"] == 1
        path.unlink()  # closed record outlives the open card
        monkeypatch.setattr(public_memory, "_source_reader", PublicMemorySource(source.path))
        restarted = knowledge.KnowledgeMiddleware(None)
        context = restarted.compose_context({"session_id": "peer", "messages": []}, record=False)
        if is_federation:
            # ADR 0066 withholds even verified public prior-session content.
            assert "rocket launch" not in context["context"] and "PRIVATE" not in context["context"]
        else:
            assert "rocket launch" in context["context"] and "PRIVATE" not in context["context"]
        source.path.unlink()  # failed source read must also invalidate cached delivery
        memory._persist_session({"session_id": "chat-1", "incognito": False,
                                 "messages": [HumanMessage(content="PRIVATE-UNKNOWN")]}, "trace")
        assert not path.exists()
        assert memory.load_digest(memory_dir=str(tmp_path)).entries == []
        assert "rocket launch" not in restarted.compose_context({"session_id": "peer", "messages": []}, record=False)["context"]

def test_caller_independent_reader_keeps_private_out_harvest(tmp_path, monkeypatch):
    """The store honours ``include_private`` as passed; it does not look at the caller.

    No product path derives the flag from the caller: the only product caller of
    ``HistoryStore.list_for_session`` (``graph/conversation_harvest.py``) passes True and
    filters by audience itself. So this asserts the flag's two values, not a per-caller rule.
    """
    from knowledge.history import HistoryStore
    from runtime.state import STATE
    store = HistoryStore(tmp_path / "kb.db")
    monkeypatch.setattr(STATE, "history_store", store)
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path / "memory"))
    _src_harvest(tmp_path, store)
    public_only = store.list_for_session("chat-9", kinds=["archive"])  # the default is include_private=False
    assert public_only == store.list_for_session("chat-9", kinds=["archive"], include_private=False)
    assert [a.content for a in public_only] == ["[Conversation archive] Earlier we compared teal and slate palettes."]
    assert all(_ARCHIVE_CANARY not in a.content for a in public_only)
    with_private = store.list_for_session("chat-9", kinds=["archive"], include_private=True)
    assert len(with_private) == 2  # both the public and the retained-private span
    assert sum(_ARCHIVE_CANARY in a.content for a in with_private) == 1

async def test_caller_independent_reader_keeps_private_out_tracing(session_langfuse, monkeypatch):
    await _src_tracing(session_langfuse, monkeypatch)

def test_private_partition_captures_nothing_per_caller_kind(caller, monkeypatch):
    from fastapi import FastAPI
    from operator_api.prompt_routes import register_prompt_routes
    from graph.middleware.request_context import request_metadata_scope
    with _caller_scope(caller):
        _src_prompt(monkeypatch)
    monkeypatch.setattr("observability.tracing.current_session_id", lambda: "chat-public")
    with request_metadata_scope({"a2a.task_id": "task-public"}):
        _run_chained(_Req("gpt-x", SystemMessage(content="PUBLIC_CONTROL")), _response())
    app = FastAPI()
    register_prompt_routes(app)
    app.add_middleware(auth.A2AAuthMiddleware)
    with TestClient(app) as client:
        private, public, latest = [client.get(f"/api/prompts/{path}", headers={"Authorization": f"Bearer {caller.token}"})
                                   for path in ("task-private", "task-public", "last?session_id=chat-public")]
    for response in (private, public, latest):
        assert all(canary not in response.text for canary in ("PRIVATE_CONTEXT_CANARY", "PRIVATE_WIRE_CANARY"))
    if caller.ctx.state["principal_kind"] == "federation":
        assert [r.status_code for r in (private, public, latest)] == [403, 403, 403]
    else:
        assert private.status_code == 404 and private.json()["detail"] == "no prompt snapshots for that task"
        assert public.status_code == latest.status_code == 200
        assert public.json()["calls"][0]["system"]["stable"] == "PUBLIC_CONTROL"
        assert latest.json()["call"]["system"]["stable"] == "PUBLIC_CONTROL"

@pytest.mark.parametrize("context_id", ["sess-1", "background:proof-1"])
async def test_caller_independent_reader_keeps_private_out_export(tmp_path, monkeypatch, context_id):
    await _src_export(tmp_path, monkeypatch, context_id)

def test_only_the_per_caller_proofs_take_a_caller():
    """Guards the gate's per-caller count: a caller-blind reader must not be run per kind."""
    takes_caller = {name for name, fn in globals().items()
                    if name.startswith("test_") and inspect.isfunction(fn) and fn.__module__ == __name__
                    and "caller" in inspect.signature(fn).parameters}
    assert takes_caller == {"test_public_projection_filters_prompts_per_caller_kind",
                            "test_private_partition_captures_nothing_per_caller_kind"}
    independent = {name for name in globals() if name.startswith("test_caller_independent_reader_keeps_private_out_")}
    assert {name.rsplit("_", 1)[1] for name in independent} == {"gist", "tracing", "export", "harvest"}
    assert not independent & takes_caller


# Digest helpers copied locally: its caller-specific delivery assertion differs.

def _summary(session_id: str, topic: str, *, gist: str = "") -> dict:
    summary = {
        "session_id": session_id,
        "trace_id": f"trace-{session_id}",
        "messages": [{"role": "user", "content": topic}],
        "final_output": f"done: {topic}",
        "timestamp": "2026-08-28T12:00:00+00:00",
    }
    if gist:
        summary["gist"] = gist
    return summary

def _write(memory_dir: Path, session_id: str, topic: str, *, mtime: float | None = None, gist: str = "") -> None:
    from graph.middleware.memory import session_filename

    path = memory_dir / session_filename(session_id)
    path.write_text(json.dumps(_summary(session_id, topic, gist=gist)), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))

def _quiet_working_state(monkeypatch) -> None:
    monkeypatch.setattr("graph.projection.working_state_block", lambda state: "")
