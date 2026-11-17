"""K2 caller-kind proofs; strict xfails expose admission/delivery gaps."""

from collections import namedtuple
from contextlib import contextmanager

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tests.test_conversation_harvest import _ARCHIVE_CANARY, test_harvest_excludes_private_canaries_from_every_derived_reader as _src_harvest
from tests.test_digest_gating import _write
from tests.test_prompt_capture_middleware import test_private_partition_captures_nothing_and_drops_the_private_buffers as _src_prompt
from tests.test_session_gist import harness, test_private_source_survives_public_turn_compaction_restart_and_stale_timer as _src_gist  # noqa: F401
from tests.test_steer_idle_starts_turn import _partition
from tests.test_steering import _clear_queue, test_no_model_call_mixes_audiences_in_a_real_graph as _src_no_mix  # noqa: F401  (autouse: resets the steering queue)
from tests.test_trace_export import _reset, test_later_public_terminal_export_uses_only_public_bytes as _src_export  # noqa: F401  (autouse: resets fleet export)
from tests.test_tracing import _restore_tracing_module, session_langfuse, test_a_private_partition_records_no_content_now_or_on_a_later_public_turn as _src_tracing  # noqa: F401

# Auth fixtures exercise the platform-branching device registry.
pytestmark = pytest.mark.platform_sensitive
# case, resolved kind, operator access; ids name the four proof-matrix callers.
CALLER_KINDS = [
    pytest.param(("federation", "federation", False), id="federation"),
    pytest.param(("fleet", "fleet-service", False), id="fleet-service"),
    pytest.param(("agent", "agent-device", False), id="paired-agent"),
    pytest.param(("device", "device", True), id="enrolled-console"),
]
_Caller = namedtuple("_Caller", "ctx principal is_operator")

@contextmanager
def _caller_scope(caller):
    from graph.middleware.request_context import approval_authority_scope, verified_principal_scope
    with verified_principal_scope(caller.ctx.state["principal_kind"]), approval_authority_scope(caller.ctx.state["trust_tier"] != "federation"):
        yield

@pytest.fixture
def queue_caller(caller, request):
    if not caller.is_operator:
        request.node.add_marker(pytest.mark.xfail(
            strict=True, raises=PermissionError,
            reason="PROOF FAILURE: scheduler admission requires a human operator; authenticated peer cannot queue",
        ))
    return caller

def _resolve(case):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from a2a_impl import auth
    from a2a_impl.stores import tier_context_builder
    from security import devices
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
    return holder["ctx"], device_id

@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    from a2a_impl import auth
    from security import devices
    monkeypatch.setattr(devices, "_registry_path", lambda: tmp_path / "caller-devices.json")
    cells = (auth._BEARER, auth._FEDERATION, auth._FLEET, auth._API_KEY, auth._ALLOWED_ORIGINS)
    snap = [cell[0] for cell in cells]
    auth.configure(bearer_token="shared", federation_token="fed", fleet_token="fleet", api_key="legacy", allowed_origins_raw="")
    yield
    for cell, value in zip(cells, snap):
        cell[0] = value

@pytest.fixture(params=CALLER_KINDS)
def caller(request, auth_env):
    from a2a_impl.auth import is_human_operator
    case, expected_kind, is_operator = request.param
    ctx, device_id = _resolve(case)
    assert ctx.state["principal_kind"] == expected_kind
    assert is_human_operator(ctx.state["principal_kind"]) is is_operator
    principal = f"{expected_kind}:{device_id}" if device_id else expected_kind
    return _Caller(ctx, principal, is_operator)

@pytest.fixture
def server_op(auth_env):
    """Resolve private-side operator auth before entering an async test loop."""
    ctx, _ = _resolve("shared")
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

async def test_cross_audience_steer_queues_behind_running_segment_per_caller_kind(queue_caller, server_op, partition):
    from a2a_impl import segment_schedule as ss
    priv = await ss.accept_pending_turn(partition, session_id="chat-q", audience=ss.AUDIENCE_PRIVATE, principal="shared-operator-bearer", dedupe="m1", input_ref="private-input:x", context=server_op)
    gen = await ss.claim_segment(partition, "chat-q", audience=ss.AUDIENCE_PRIVATE, seq=priv["seq"], context=server_op)
    await ss.set_turn_state(partition, "chat-q", priv["seq"], ss.STATE_RUNNING, server_op)
    await ss.accept_pending_turn(partition, session_id="chat-q", audience=ss.AUDIENCE_PEER, principal=queue_caller.principal, dedupe="m2", context=queue_caller.ctx)
    assert await ss.try_fold(partition, "chat-q", generation=gen, audience=ss.AUDIENCE_PEER, seq=2, context=server_op) is None
    assert await ss.next_dispatch(partition, "chat-q", server_op) is None  # reserved by the private segment
    assert await ss.complete_segment(partition, "chat-q", generation=gen, context=server_op) is True
    nxt = await ss.next_dispatch(partition, "chat-q", server_op)
    assert nxt["dedupe"] == "m2" and nxt["audience"] == "peer"

async def test_duplicate_message_id_gives_one_pending_turn_per_caller_kind(queue_caller, partition):
    from sqlalchemy import text as sql
    from a2a_impl import segment_schedule as ss
    first = await ss.accept_pending_turn(partition, session_id="chat-d", audience=ss.AUDIENCE_PEER, principal=queue_caller.principal, dedupe="m1", context=queue_caller.ctx)
    again = await ss.accept_pending_turn(partition, session_id="chat-d", audience=ss.AUDIENCE_PEER, principal=queue_caller.principal, dedupe="m1", context=queue_caller.ctx)
    assert again["seq"] == first["seq"]  # the retry reuses the one turn
    async with partition.engine.connect() as conn:
        n = (await conn.execute(sql("SELECT COUNT(*) FROM tasks WHERE public_visible=0"))).scalar()
    assert n == 1  # one record, not two

def test_private_source_survives_public_turn_per_caller_kind(caller, harness, tmp_path, monkeypatch):  # noqa: F811
    with _caller_scope(caller):
        _src_gist(harness, tmp_path, monkeypatch)

@pytest.mark.parametrize("provenance", [None, {"audience": "mixed", "version": "1"}, {"audience": "public", "version": "old"}])
def test_public_projection_filters_prompts_per_caller_kind(caller, tmp_path, monkeypatch, provenance, request):
    if caller.ctx.state["principal_kind"] == "federation":
        request.node.add_marker(pytest.mark.xfail(
            strict=True, raises=_DigestDeliveryFailure,
            reason="PROOF FAILURE: federation suppresses even public prior-session digest delivery",
        ))
    with _caller_scope(caller):
        _digest_scenario(tmp_path, monkeypatch, provenance)

def test_harvest_excludes_private_canaries_per_caller_kind(caller, tmp_path, monkeypatch):
    from a2a_impl.auth import is_human_operator
    from graph.middleware.request_context import current_verified_principal
    from knowledge.history import HistoryStore
    from runtime.state import STATE
    store = HistoryStore(tmp_path / "kb.db")
    monkeypatch.setattr(STATE, "history_store", store)
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path / "memory"))
    with _caller_scope(caller):
        _src_harvest(tmp_path, store)
        arcs = store.list_for_session("chat-9", kinds=["archive"], include_private=is_human_operator(current_verified_principal()))
    if caller.is_operator:
        assert len(arcs) == 2  # the console recovers both the public and retained-private spans
    else:
        assert [a.content for a in arcs] == ["[Conversation archive] Earlier we compared teal and slate palettes."]
        assert all(_ARCHIVE_CANARY not in a.content for a in arcs)

async def test_private_partition_records_no_trace_content_per_caller_kind(caller, session_langfuse, monkeypatch):  # noqa: F811
    with _caller_scope(caller):
        await _src_tracing(session_langfuse, monkeypatch)

def test_private_partition_captures_nothing_per_caller_kind(caller, monkeypatch):
    with _caller_scope(caller):
        _src_prompt(monkeypatch)

@pytest.mark.parametrize("context_id", ["sess-1", "background:proof-1"])
async def test_later_public_terminal_export_per_caller_kind(caller, server_op, partition, tmp_path, monkeypatch, context_id):
    from a2a.types import a2a_pb2
    from langchain_core.messages import messages_from_dict, messages_to_dict
    task = a2a_pb2.Task(id="task-c", context_id=context_id)
    await partition.save_private(task, {"messages": messages_to_dict([HumanMessage(content="PRIVATE_STEERING_CANARY")]), "summary": "PRIVATE_SUMMARY_CANARY"}, server_op)
    recovered = (await partition.get_record(task.id, server_op)).private_payload
    assert messages_from_dict(recovered["messages"])[0].content == "PRIVATE_STEERING_CANARY"
    as_caller = await partition.get_record(task.id, caller.ctx)
    if caller.is_operator:
        assert as_caller is not None
        assert messages_from_dict(as_caller.private_payload["messages"])[0].content == "PRIVATE_STEERING_CANARY"
    else:
        assert as_caller is None or as_caller.private_payload is None
    with _caller_scope(caller):
        await _src_export(tmp_path, monkeypatch, context_id)

class _DigestDeliveryFailure(AssertionError):
    """Only the public-digest positive control failed; leak assertions still passed."""

def _digest_scenario(tmp_path, monkeypatch, provenance):
    import json
    from datetime import datetime, timezone
    from graph import public_memory
    from graph.middleware import memory
    from graph.middleware.knowledge import KnowledgeMiddleware
    from graph.session_search import search_session_summaries
    from tests._public_memory_source import PublicMemorySource
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(memory, "_PERSISTENCE_DISABLED", False)
    monkeypatch.setattr("graph.projection.working_state_block", lambda state: "")
    _write(tmp_path, "chat-1", "PRIVATE-CANARY", gist="PRIVATE-GIST")
    path = tmp_path / "chat-1.json"
    old = json.loads(path.read_text())
    if provenance is not None:
        old[public_memory.PROVENANCE] = provenance
    path.write_text(json.dumps(old))
    mw = KnowledgeMiddleware(None)
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
    assert not memory.record_closed_session("chat-1", topic="PRIVATE", gist="PRIVATE", closed_at=now, source_card=old, audience="public", source_version=1)
    for policy in ("newest", "relevant"):
        assert memory.load_digest(policy, query="PRIVATE", memory_dir=str(tmp_path)).entries == []
    assert "PRIVATE" not in (mw.compose_context({"session_id": "peer", "messages": []}, record=False) or {}).get("context", "")
    memory._persist_session({"session_id": "chat-1", "incognito": False, "messages": [HumanMessage(content="PRIVATE-COMPACTED"), AIMessage(content="PRIVATE-REPLY")]}, "trace")
    card = json.loads(path.read_text())
    assert "PRIVATE" not in json.dumps(card)
    for policy in ("newest", "relevant"):
        assert "rocket launch" in memory.load_digest(policy, query="rocket", memory_dir=str(tmp_path)).block
    card.update(audience="public", source_version=1)
    assert memory.record_closed_session("chat-1", topic="ignored", gist="ignored", closed_at=now, source_card=card, audience="mixed", source_version=0)
    closed_card = json.loads((closed / path.name).read_text())
    assert closed_card["audience"] == "public" and closed_card["source_version"] == 1
    path.unlink()  # closed record outlives the open card
    monkeypatch.setattr(public_memory, "_source_reader", PublicMemorySource(source.path))
    restarted = KnowledgeMiddleware(None)
    context = restarted.compose_context({"session_id": "peer", "messages": []}, record=False)
    # Defer only the positive control so federation still runs every leak/cache check.
    assert "PRIVATE" not in context["context"]
    delivered = "rocket launch" in context["context"]
    source.path.unlink()  # failed source read must also invalidate cached delivery
    memory._persist_session({"session_id": "chat-1", "incognito": False, "messages": [HumanMessage(content="PRIVATE-UNKNOWN")]}, "trace")
    assert not path.exists()
    assert memory.load_digest(memory_dir=str(tmp_path)).entries == []
    assert "rocket launch" not in restarted.compose_context({"session_id": "peer", "messages": []}, record=False)["context"]
    if not delivered:
        raise _DigestDeliveryFailure("Public digest was not delivered")
