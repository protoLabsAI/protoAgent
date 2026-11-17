"""P1 proof K2: caller-kind runs for steering queues and derived readers (TESTS ONLY).

The proof matrix (docs/design/conversations-in-work-proposal.md ~line 648) requires every
identity-sensitive queue and derived-reader scenario to hold for federation, fleet-service,
paired-agent and enrolled-console callers — "no operator-tier shortcut". Each kind's principal is
resolved through the REAL auth path (``A2AAuthMiddleware`` + ``tier_context_builder()``), and every
named source scenario runs once per kind via the parametrized ``caller`` fixture.

Identity-independent readers (gist, digest, tracing, prompt capture) and the export's clean bytes
drop private content at PROJECTION, not read, so the untouched source scenario (original assertions
verbatim) is re-run per resolved caller — only the enrolled console reads as a human operator. The
three identity surfaces thread the caller in: the peer steer carries ``caller.principal``; the
archive is read with ``include_private=caller.is_operator``; ``get_record`` is read via ``caller.ctx``.

Source helpers and scenarios are IMPORTED, never edited; nothing under ``src/`` changes.
"""

from __future__ import annotations

from collections import namedtuple

import pytest
from langchain_core.messages import HumanMessage

# Source scenarios — imported and re-run verbatim under each caller kind (aliased so pytest does
# not re-collect them here).
from tests.test_conversation_harvest import _ARCHIVE_CANARY
from tests.test_conversation_harvest import test_harvest_excludes_private_canaries_from_every_derived_reader as _src_harvest
from tests.test_digest_gating import test_public_projection_filters_open_closed_relevant_and_cached_prompts as _src_digest
from tests.test_prompt_capture_middleware import test_private_partition_captures_nothing_and_drops_the_private_buffers as _src_prompt
from tests.test_session_gist import test_private_source_survives_public_turn_compaction_restart_and_stale_timer as _src_gist
from tests.test_steer_idle_starts_turn import _partition
from tests.test_steering import test_no_model_call_mixes_audiences_in_a_real_graph as _src_no_mix
from tests.test_trace_export import test_later_public_terminal_export_uses_only_public_bytes as _src_export
from tests.test_tracing import test_a_private_partition_records_no_content_now_or_on_a_later_public_turn as _src_tracing

# Autouse fixtures the imported scenarios rely on, plus the fixtures they request by name.
from tests.test_session_gist import harness  # noqa: F401
from tests.test_steering import _clear_queue  # noqa: F401  (autouse: resets the steering queue)
from tests.test_trace_export import _reset  # noqa: F401  (autouse: resets fleet export)
from tests.test_tracing import _restore_tracing_module, session_langfuse  # noqa: F401

# Device/agent pairing writes the OS-sensitive device registry, so these proofs ride Windows CI.
pytestmark = pytest.mark.platform_sensitive

# kind -> (auth classification case, resolved principal_kind, reads as human operator?).
_CASE_FOR_KIND = {
    "federation": ("federation", "federation", False),
    "fleet-service": ("fleet", "fleet-service", False),
    "paired-agent": ("agent", "agent-device", False),
    "enrolled-console": ("device", "device", True),
}
CALLER_KINDS = list(_CASE_FOR_KIND)
_Caller = namedtuple("_Caller", "kind ctx principal is_operator")


def _resolve(case):
    """Resolve ONE caller's principal through the real server auth path; return its
    ``ServerCallContext`` + paired device id (``""`` for token credentials)."""
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
    """Configure the real auth ladder and point the device registry at a tmp file so each caller
    kind resolves through it; snapshot/restore the process-global auth tokens."""
    from a2a_impl import auth
    from security import devices

    reg_dir = tmp_path / "caller_auth"
    reg_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(devices, "_registry_path", lambda: reg_dir / "devices.json")
    snap = (auth._BEARER[0], auth._FEDERATION[0], auth._FLEET[0], auth._API_KEY[0], auth._ALLOWED_ORIGINS[0])
    auth.configure(bearer_token="shared", federation_token="fed", fleet_token="fleet", api_key="legacy", allowed_origins_raw="")
    yield
    (auth._BEARER[0], auth._FEDERATION[0], auth._FLEET[0], auth._API_KEY[0], auth._ALLOWED_ORIGINS[0]) = snap


@pytest.fixture(params=CALLER_KINDS)
def caller(request, auth_env):
    """The caller kind under test, resolved through the real auth path (no operator shortcut)."""
    from a2a_impl.auth import is_human_operator

    case, expected_kind, is_operator = _CASE_FOR_KIND[request.param]
    ctx, device_id = _resolve(case)
    assert ctx.state["principal_kind"] == expected_kind
    assert is_human_operator(ctx.state["principal_kind"]) is is_operator
    principal = f"{expected_kind}:{device_id}" if device_id else expected_kind
    return _Caller(request.param, ctx, principal, is_operator)


@pytest.fixture
def server_op(auth_env):
    """The human operator the private-partition machinery runs under, resolved via the real auth
    path. Resolved at setup so its sync portal never collides with an async test's running loop."""
    ctx, _ = _resolve("shared")
    assert ctx.state["principal_kind"] == "shared-operator-bearer"
    return ctx


# ── Queues — audience isolation holds for every caller kind ──────────────────────


@pytest.mark.parametrize("segment_audience", ["peer", "private"])
async def test_no_model_call_mixes_audiences_per_caller_kind(caller, segment_audience):
    """test_steering::test_no_model_call_mixes_audiences_in_a_real_graph per resolved caller: the
    segment's fixed audience — never the caller — decides what the next model call sees."""
    await _src_no_mix(segment_audience)


async def test_cross_audience_steer_queues_behind_running_segment_per_caller_kind(caller, server_op, tmp_path):
    """test_steer_idle_starts_turn::test_a_cross_audience_steer_queues_behind_the_running_segment,
    peer steer SUBMITTED BY THIS CALLER: it must not fold into the running private segment."""
    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = server_op
    try:
        priv = await ss.accept_pending_turn(store, session_id="chat-q", audience=ss.AUDIENCE_PRIVATE,
                                             principal="shared-operator-bearer", dedupe="m1", input_ref="private-input:x", context=op)
        gen = await ss.claim_segment(store, "chat-q", audience=ss.AUDIENCE_PRIVATE, seq=priv["seq"], context=op)
        await ss.set_turn_state(store, "chat-q", priv["seq"], ss.STATE_RUNNING, op)
        await ss.accept_pending_turn(store, session_id="chat-q", audience=ss.AUDIENCE_PEER,
                                     principal=caller.principal, dedupe="m2", context=op)
        assert await ss.try_fold(store, "chat-q", generation=gen, audience=ss.AUDIENCE_PEER, seq=2, context=op) is None
        assert await ss.next_dispatch(store, "chat-q", op) is None  # reserved by the private segment
        assert await ss.complete_segment(store, "chat-q", generation=gen, context=op) is True
        nxt = await ss.next_dispatch(store, "chat-q", op)
        assert nxt["dedupe"] == "m2" and nxt["audience"] == "peer"
    finally:
        await engine.dispose()


async def test_duplicate_message_id_gives_one_pending_turn_per_caller_kind(caller, server_op, tmp_path):
    """test_steer_idle_starts_turn::test_duplicate_message_id_gives_exactly_one_pending_turn, retried
    id SUBMITTED BY THIS CALLER: it reuses the one pending turn — one durable turn per message id."""
    from sqlalchemy import text as sql

    from a2a_impl import segment_schedule as ss

    engine, store = await _partition(tmp_path)
    op = server_op
    try:
        first = await ss.accept_pending_turn(store, session_id="chat-d", audience=ss.AUDIENCE_PEER,
                                             principal=caller.principal, dedupe="m1", context=op)
        again = await ss.accept_pending_turn(store, session_id="chat-d", audience=ss.AUDIENCE_PEER,
                                             principal=caller.principal, dedupe="m1", context=op)
        assert again["seq"] == first["seq"]  # the retry reuses the one turn
        async with store.engine.connect() as conn:
            n = (await conn.execute(sql("SELECT COUNT(*) FROM tasks WHERE public_visible=0"))).scalar()
        assert n == 1  # one record, not two
    finally:
        await engine.dispose()


# ── Derived readers — public-only for every caller kind ──────────────────────────


def test_private_source_survives_public_turn_per_caller_kind(caller, harness, tmp_path, monkeypatch):  # noqa: F811
    """test_session_gist::...survives_public_turn_compaction_restart_and_stale_timer per resolved
    caller: the idle gist is a public projection with no per-caller read surface — no canary."""
    _src_gist(harness, tmp_path, monkeypatch)


@pytest.mark.parametrize("provenance", [None, {"audience": "mixed", "version": "1"}, {"audience": "public", "version": "old"}])
def test_public_projection_filters_prompts_per_caller_kind(caller, tmp_path, monkeypatch, provenance):
    """test_digest_gating::...filters_open_closed_relevant_and_cached_prompts per resolved caller:
    the digest / composed peer prompt is a public projection — the canary is filtered for any kind."""
    _src_digest(tmp_path, monkeypatch, provenance)


def test_harvest_excludes_private_canaries_per_caller_kind(caller, tmp_path, monkeypatch):
    """test_conversation_harvest::...excludes_private_canaries_from_every_derived_reader per resolved
    caller (original assertions verbatim); the retained archive is recovered only by the console."""
    from knowledge.history import HistoryStore
    from runtime.state import STATE

    store = HistoryStore(tmp_path / "kb.db")
    monkeypatch.setattr(STATE, "history_store", store)
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path / "memory"))
    _src_harvest(tmp_path, store)
    arcs = store.list_for_session("chat-9", kinds=["archive"], include_private=caller.is_operator)
    if caller.is_operator:
        assert len(arcs) == 2  # the console recovers both the public and retained-private spans
    else:
        assert [a.content for a in arcs] == ["[Conversation archive] Earlier we compared teal and slate palettes."]
        assert all(_ARCHIVE_CANARY not in a.content for a in arcs)


async def test_private_partition_records_no_trace_content_per_caller_kind(caller, session_langfuse, monkeypatch):  # noqa: F811
    """test_tracing::...records_no_content_now_or_on_a_later_public_turn per resolved caller:
    suppression follows the retained source, not the reader — no kind reads a canary off the trace."""
    await _src_tracing(session_langfuse, monkeypatch)


def test_private_partition_captures_nothing_per_caller_kind(caller, monkeypatch):
    """test_prompt_capture_middleware::...captures_nothing_and_drops_the_private_buffers per resolved
    caller: a private partition is suppressed before capture and its buffers dropped — none can pop."""
    _src_prompt(monkeypatch)


@pytest.mark.parametrize("context_id", ["sess-1", "background:proof-1"])
async def test_later_public_terminal_export_per_caller_kind(caller, server_op, tmp_path, monkeypatch, context_id):
    """test_trace_export::...uses_only_public_bytes per resolved caller (original clean-bytes
    assertions verbatim); the operator writes the private partition and recovers it, peers denied."""
    from a2a.types import a2a_pb2
    from langchain_core.messages import messages_from_dict, messages_to_dict

    engine, store = await _partition(tmp_path)
    try:
        task = a2a_pb2.Task(id="task-c", context_id=context_id)
        private = [HumanMessage(content="PRIVATE_STEERING_CANARY")]
        await store.save_private(task, {"messages": messages_to_dict(private), "summary": "PRIVATE_SUMMARY_CANARY"}, server_op)
        recovered = (await store.get_record(task.id, server_op)).private_payload
        assert messages_from_dict(recovered["messages"])[0].content == "PRIVATE_STEERING_CANARY"
        as_caller = await store.get_record(task.id, caller.ctx)
        if caller.is_operator:
            assert as_caller is not None
            assert messages_from_dict(as_caller.private_payload["messages"])[0].content == "PRIVATE_STEERING_CANARY"
        else:
            assert as_caller is None or as_caller.private_payload is None
    finally:
        await engine.dispose()
    await _src_export(tmp_path, monkeypatch, context_id)
