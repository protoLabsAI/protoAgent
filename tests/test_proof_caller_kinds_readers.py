"""K2 isolation proofs for four caller kinds resolved through production authentication."""

import json
from types import SimpleNamespace
from contextlib import contextmanager

import pytest
from langchain_core.messages import SystemMessage
from starlette.testclient import TestClient
from a2a_impl import auth, segment_schedule as ss
from security import devices

from tests.test_conversation_harvest import _ARCHIVE_CANARY, test_harvest_excludes_private_canaries_from_every_derived_reader as _src_harvest
from tests.test_digest_gating import _quiet_working_state, _write
from tests.test_prompt_capture_middleware import _Req, _response, _run_chained, test_private_partition_captures_nothing_and_drops_the_private_buffers as _src_prompt
from tests.test_session_gist import harness, test_private_source_survives_public_turn_compaction_restart_and_stale_timer as _src_gist  # noqa: F401
from tests.test_steer_idle_starts_turn import _partition
from tests.test_steering import _clear_queue, test_no_model_call_mixes_audiences_in_a_real_graph as _src_no_mix  # noqa: F401  (autouse: resets the steering queue)
from tests.test_trace_export import _reset, test_later_public_terminal_export_uses_only_public_bytes as _src_export  # noqa: F401  (autouse: resets fleet export)
from tests.test_tracing import _restore_tracing_module, session_langfuse, test_a_private_partition_records_no_content_now_or_on_a_later_public_turn as _src_tracing  # noqa: F401

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

class _QueueAdmissionFailure(AssertionError):
    """Only the caller's scheduler admission failed, not private-side setup."""

class _DigestDeliveryFailure(AssertionError):
    """The original public positive control is withheld by federation policy."""

async def _accept_peer(caller, request, partition, **kwargs):
    if not caller.is_operator:
        request.node.add_marker(pytest.mark.xfail(strict=True, raises=_QueueAdmissionFailure,
            reason="PROOF FAILURE: non-operator scheduler admission prevents queue isolation/dedupe proof"))
    try:
        return await ss.accept_pending_turn(partition, audience=ss.AUDIENCE_PEER,
                                           principal=caller.principal, context=caller.ctx, **kwargs)
    except PermissionError as exc:
        if caller.is_operator or str(exc) != "Private task access requires a human operator":
            raise
        raise _QueueAdmissionFailure(str(exc)) from exc

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

@pytest.fixture
def server_op(auth_env):
    ctx, _, _ = _resolve("shared")
    assert ctx.state["principal_kind"] == "shared-operator-bearer"
    return ctx

@pytest.fixture
async def partition(tmp_path):
    engine, store = await _partition(tmp_path)
    try:
        yield store
    finally:
        await engine.dispose()

@pytest.mark.parametrize("segment_audience", ["peer", "private"])
async def test_no_model_call_mixes_audiences_per_caller_kind(caller, segment_audience):
    with _caller_scope(caller):
        await _src_no_mix(segment_audience)

async def test_cross_audience_steer_queues_behind_running_segment_per_caller_kind(caller, request, server_op, partition):
    priv = await ss.accept_pending_turn(partition, session_id="chat-q", audience=ss.AUDIENCE_PRIVATE, principal="shared-operator-bearer", dedupe="m1", input_ref="private-input:x", context=server_op)
    gen = await ss.claim_segment(partition, "chat-q", audience=ss.AUDIENCE_PRIVATE, seq=priv["seq"], context=server_op)
    await ss.set_turn_state(partition, "chat-q", priv["seq"], ss.STATE_RUNNING, server_op)
    await _accept_peer(caller, request, partition, session_id="chat-q", dedupe="m2")
    assert await ss.try_fold(partition, "chat-q", generation=gen, audience=ss.AUDIENCE_PEER, seq=2, context=server_op) is None
    assert await ss.next_dispatch(partition, "chat-q", server_op) is None  # reserved by the private segment
    assert await ss.complete_segment(partition, "chat-q", generation=gen, context=server_op) is True
    nxt = await ss.next_dispatch(partition, "chat-q", server_op)
    assert nxt["dedupe"] == "m2" and nxt["audience"] == "peer"

async def test_duplicate_message_id_gives_one_pending_turn_per_caller_kind(caller, request, partition):
    from sqlalchemy import text as sql
    first = await _accept_peer(caller, request, partition, session_id="chat-d", dedupe="m1")
    again = await ss.accept_pending_turn(partition, session_id="chat-d", audience=ss.AUDIENCE_PEER, principal=caller.principal, dedupe="m1", context=caller.ctx)
    assert again["seq"] == first["seq"]  # the retry reuses the one turn
    async with partition.engine.connect() as conn:
        n = (await conn.execute(sql("SELECT COUNT(*) FROM tasks WHERE public_visible=0"))).scalar()
    assert n == 1  # one record, not two

def test_private_source_survives_public_turn_per_caller_kind(caller, harness, tmp_path, monkeypatch):  # noqa: F811
    with _caller_scope(caller):
        _src_gist(harness, tmp_path, monkeypatch)

@pytest.mark.parametrize("provenance", [None, {"audience": "mixed", "version": "1"}, {"audience": "public", "version": "old"}])
def test_public_projection_filters_prompts_per_caller_kind(caller, request, tmp_path, monkeypatch, provenance):
    from graph.middleware import knowledge, memory
    with _caller_scope(caller):
        is_federation = caller.ctx.state["principal_kind"] == "federation"
        assert knowledge._restrict_prior_session_memory() is is_federation
        if is_federation:
            request.node.add_marker(pytest.mark.xfail(strict=True, raises=_DigestDeliveryFailure,
                reason="PROOF FAILURE: federation policy withholds the source scenario's public digest positive control"))
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
        # Defer only federation delivery failure so stale-source checks still run.
        assert "PRIVATE" not in context["context"]
        delivered = "rocket launch" in context["context"]
        assert delivered or is_federation
        source.path.unlink()  # failed source read must also invalidate cached delivery
        memory._persist_session({"session_id": "chat-1", "incognito": False,
                                 "messages": [HumanMessage(content="PRIVATE-UNKNOWN")]}, "trace")
        assert not path.exists()
        assert memory.load_digest(memory_dir=str(tmp_path)).entries == []
        assert "rocket launch" not in restarted.compose_context({"session_id": "peer", "messages": []}, record=False)["context"]
        if not delivered:
            raise _DigestDeliveryFailure("federation policy withholds the public digest positive control")

def test_harvest_excludes_private_canaries_per_caller_kind(caller, tmp_path, monkeypatch):
    from graph.middleware.request_context import current_verified_principal
    from knowledge.history import HistoryStore
    from runtime.state import STATE
    store = HistoryStore(tmp_path / "kb.db")
    monkeypatch.setattr(STATE, "history_store", store)
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path / "memory"))
    with _caller_scope(caller):
        _src_harvest(tmp_path, store)
        arcs = store.list_for_session("chat-9", kinds=["archive"], include_private=auth.is_human_operator(current_verified_principal()))
    if caller.is_operator:
        assert len(arcs) == 2  # the console recovers both the public and retained-private spans
    else:
        assert [a.content for a in arcs] == ["[Conversation archive] Earlier we compared teal and slate palettes."]
        assert all(_ARCHIVE_CANARY not in a.content for a in arcs)

async def test_private_partition_records_no_trace_content_per_caller_kind(caller, session_langfuse, monkeypatch):  # noqa: F811
    with _caller_scope(caller):
        await _src_tracing(session_langfuse, monkeypatch)

def test_private_partition_captures_nothing_per_caller_kind(caller, monkeypatch):
    from fastapi import FastAPI
    from operator_api.prompt_routes import register_prompt_routes
    from graph.middleware.request_context import request_metadata_scope
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
async def test_later_public_terminal_export_per_caller_kind(caller, tmp_path, monkeypatch, context_id):
    with _caller_scope(caller):
        await _src_export(tmp_path, monkeypatch, context_id)
