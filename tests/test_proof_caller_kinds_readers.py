"""K2 derived-reader proofs for callers resolved through production authentication.

Steering scenarios moved to P1h.3 (bd-o4z6e): there is no caller-to-queue path yet.
The copied reader scenarios preserve their assertions; only digest delivery differs
for federation, whose prior-session gate stays enabled per ADR 0066.
"""

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from starlette.testclient import TestClient

from a2a_impl import auth
from graph import session_gist
from graph.config import LangGraphConfig
from graph.conversation_harvest import harvest_thread
from graph.middleware import memory
from graph.middleware.prompt_cache import PromptCacheMiddleware
from graph.middleware.prompt_capture import PromptCaptureMiddleware
from graph.middleware.request_context import request_metadata_scope
from knowledge.history import HistoryStore
from observability import trace_export
from observability.prompt_snapshots import prompt_snapshots
from security import devices

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

def test_private_source_survives_public_turn_per_caller_kind(caller, harness, tmp_path, monkeypatch):
    with _caller_scope(caller):
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

async def test_private_partition_records_no_trace_content_per_caller_kind(caller, session_langfuse, monkeypatch):
    with _caller_scope(caller):
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
async def test_later_public_terminal_export_per_caller_kind(caller, tmp_path, monkeypatch, context_id):
    with _caller_scope(caller):
        await _src_export(tmp_path, monkeypatch, context_id)


# Helpers and scenario copied from tests/test_conversation_harvest.py.

_STEER_CANARY = "STEERSECRET acquire TargetCo before Friday"

_REPLY_CANARY = "PRIVATEREPLY understood, keeping the TargetCo plan quiet"

_ARCHIVE_CANARY = "LEGACYSECRET the TargetCo valuation is 4.2B"

def _private(message):
    """Stamp a checkpoint message as belonging to the private partition (ADR 0131 §5)."""
    from graph.conversation_audience import PRIVATE_AUDIENCE

    message.additional_kwargs["audience"] = PRIVATE_AUDIENCE
    return message

def _src_harvest(tmp_path, history_store):
    """Proof slice: a later public turn (incognito=False) with retained private messages,
    a private reply, and a legacy private archive. The canaries must be absent from the
    summary, the closed-chat card, the compaction archive, shared-history search, fact
    recall and facts — while the public part still flows through."""
    import json as _json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from knowledge.memories import MemoryStore

    from graph.conversation_audience import PRIVATE_AUDIENCE, PUBLIC_AUDIENCE
    from graph.conversation_harvest import archive_payload
    from graph.middleware.memory import memory_path, session_filename

    session_id = "chat-9"
    # A legacy private archive span, retained in the private partition on disk.
    history_store.add(
        f"[Conversation archive] {_ARCHIVE_CANARY}",
        kind="archive",
        session_id=session_id,
        namespace=f"chat-archive:{session_id}",
        audience=PRIVATE_AUDIENCE,
    )
    # A public archive span from the same chat — this one DOES belong in a public summary.
    history_store.add(
        "[Conversation archive] Earlier we compared teal and slate palettes.",
        kind="archive",
        session_id=session_id,
        namespace=f"chat-archive:{session_id}",
        audience=PUBLIC_AUDIENCE,
    )

    messages = [
        HumanMessage(content="help me pick a theme for the dashboard"),
        _private(HumanMessage(content=_STEER_CANARY)),
        _private(AIMessage(content=f"<output>{_REPLY_CANARY}</output>")),
        AIMessage(content="<output>A teal palette fits the dashboard.</output>"),
    ]
    saver = SimpleNamespace(aget_tuple=AsyncMock(return_value=SimpleNamespace(checkpoint={
        "channel_values": {
            "messages": messages,
            "session_id": session_id,
            "incognito": False,  # the later ordinary turn cleared it — protection must not rely on it
            "operator_chat_turn": True,
        },
    })))

    seen_transcripts = []

    async def summarize(transcript, config):
        seen_transcripts.append(transcript)
        return "The operator wants a teal dashboard theme."

    async def facts(transcript, config):  # must never run — provenance is mixed/unknown
        raise AssertionError("fact extraction must be disabled for mixed/unknown provenance")

    memory = MemoryStore(tmp_path / "agent.db")
    cid = asyncio.run(harvest_thread(
        "a2a:chat-9", checkpointer=saver, knowledge_store=None, history_store=history_store,
        memory_store=memory, config=SimpleNamespace(knowledge_facts=True),
        summarizer=summarize, fact_extractor=facts, raise_on_error=True,
    ))

    # The public archive + public messages reached the summarizer; no canary did.
    transcript = "".join(seen_transcripts)
    assert "teal and slate palettes" in transcript and "teal palette fits" in transcript
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert canary not in transcript

    # Summary row — clean, and stamped with the public projection lineage.
    summary = history_store.get(cid)
    assert summary.kind == "summary" and summary.audience == PUBLIC_AUDIENCE and summary.source_version == 1
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert canary not in summary.content

    # Shared-history search never surfaces the private archive, nor any new canary. A
    # restart (reopen the store from disk) keeps the exclusion — the columns persist.
    for store in (history_store, HistoryStore(history_store.path)):
        # Single FTS5 tokens — history search takes FTS syntax, not a raw phrase.
        for token in ("STEERSECRET", "PRIVATEREPLY", "LEGACYSECRET", "TargetCo"):
            assert store.search(token) == []
        # The public read hides the private archive; the public archive is still listed.
        public_arcs = [a.content for a in store.list_for_session(session_id, kinds=["archive"])]
        assert public_arcs == ["[Conversation archive] Earlier we compared teal and slate palettes."]
        # The private archive is retained — a console reader (include_private) still sees both.
        assert len(store.list_for_session(session_id, kinds=["archive"], include_private=True)) == 2

    # Closed-chat digest card — topic + gist come from the public part only.
    import os as _os

    closed_file = _os.path.join(memory_path(), "closed", session_filename(session_id))
    card = _json.loads(open(closed_file, encoding="utf-8").read())
    assert card["audience"] == PUBLIC_AUDIENCE and card["source_version"] == 1
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert canary not in _json.dumps(card)

    # Compaction archive of the same mixed checkpoint drops the private messages.
    content, kwargs = archive_payload(messages, session_id=session_id, thread_id="a2a:chat-9", cause="auto-compaction")
    assert kwargs["audience"] == PUBLIC_AUDIENCE and kwargs["source_version"] == 1
    assert "teal palette fits" in content
    for canary in (_STEER_CANARY, _REPLY_CANARY):
        assert canary not in content

    # Facts / recall — extraction was disabled (mixed provenance) so nothing landed.
    assert memory.list(kind="fact") == []
    for canary in (_STEER_CANARY, _REPLY_CANARY, _ARCHIVE_CANARY):
        assert memory.search(canary) == []


# Helpers and scenario copied from tests/test_digest_gating.py.

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


# Helpers and scenario copied from tests/test_prompt_capture_middleware.py.

class _Req:
    """Minimal ModelRequest stand-in (the test_prompt_cache shape)."""

    def __init__(self, model_name, system_message, state=None):
        self.model = SimpleNamespace(model_name=model_name)
        self.system_message = system_message
        self.state = state or {}

    def override(self, **kw):
        r = _Req(self.model.model_name, self.system_message, self.state)
        for k, v in kw.items():
            setattr(r, k, v)
        return r

def _response(usage=None):
    msg = AIMessage(content="ok", usage_metadata=usage)
    return SimpleNamespace(result=[msg])

def _run_chained(req, response, capture=None, cache=None):
    """Run PromptCache wrapping PromptCapture (production order) to a canned
    response; returns what the chain returned."""
    cache = cache or PromptCacheMiddleware()
    capture = capture or PromptCaptureMiddleware()
    return cache.wrap_model_call(req, lambda r: capture.wrap_model_call(r, lambda _r: response))

def _src_prompt(monkeypatch):
    """ADR 0131 §5 (P1g.1): a conversation's private partition is suppressed before capture —
    like incognito, but it follows the retained source (so a later ordinary turn can't clear
    it) and it DROPS the private buffers (projected context + wire stash), not just the final
    write, so nothing survives for a later call to pop."""
    from langchain_core.messages import HumanMessage

    from graph import public_memory
    from graph.context_frame import pop_projected_context, stash_projected_context
    from graph.middleware import wire_capture

    source = public_memory.PublicSource(version="v2", messages=(HumanMessage(content="pub"),))
    monkeypatch.setattr(public_memory, "_source_reader", lambda sid: source if sid == "chat-private" else None)
    monkeypatch.setattr("observability.tracing.current_session_id", lambda: "chat-private")

    capture = PromptCaptureMiddleware()
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"), state={})

    def inner(r):
        stash_projected_context("PRIVATE_CONTEXT_CANARY", [{"label": "Hot", "chars": 3}])
        wire_capture._WIRE_SYSTEM.set("PRIVATE_WIRE_CANARY")
        return _response()

    with request_metadata_scope({"a2a.task_id": "task-private"}):
        capture.wrap_model_call(req, inner)

    assert prompt_snapshots().calls_for_task("task-private") == []
    # Both private buffers were dropped — a later call pops nothing.
    assert pop_projected_context() == (None, None)
    assert wire_capture.pop_wire_system() is None


# Helpers and scenario copied from tests/test_session_gist.py.

@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(memory, "_PERSISTENCE_DISABLED", False)
    monkeypatch.setattr(session_gist, "_pending", {})
    timers = []
    calls = []

    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback = delay, callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def fire(self):
            # Also exercise a cancelled callback racing its replacement.
            self.callback()

    def invoke(messages):
        calls.append(json.loads(messages[-1].content))
        return AIMessage(content="We are designing the idle session gist. Implementation is ready for review. " * 5)

    def model(config, role):
        assert role == "memory_gist"
        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(session_gist, "Timer", Timer)
    monkeypatch.setattr(session_gist, "create_role_llm", model)
    config = LangGraphConfig()
    mw = memory.SessionSummaryMiddleware(config=config)
    state = {"session_id": "chat-1", "messages": []}

    def turn(pairs=1):
        mw.before_agent(state, None)
        for _ in range(pairs):
            state["messages"].extend([HumanMessage(content="Design this feature"), AIMessage(content="Working on it")])
        mw.after_agent(state, None)

    def card():
        return json.loads((tmp_path / memory.session_filename(state["session_id"])).read_text())

    return SimpleNamespace(mw=mw, state=state, timers=timers, calls=calls, turn=turn, card=card, config=config)

def _src_gist(harness, tmp_path, monkeypatch):
    import asyncio
    from langchain_core.messages import ToolMessage
    from graph import public_memory
    from graph.compaction_op import compact_thread
    from tests._public_memory_source import PublicMemorySource

    h = harness
    # Seed an already-running legacy timer and contaminated previous gist.
    h.state["messages"] = [HumanMessage(content="PRIVATE-CANARY")] * 4
    h.turn(2)
    stale = h.timers[-1]
    path = tmp_path / "chat-1.json"
    path.write_text(json.dumps({**h.card(), "gist": "PRIVATE-OLD-GIST"}))
    source = PublicMemorySource(tmp_path / "source.record")
    public = [HumanMessage(content="rocket planning"), AIMessage(content="public answer")] * 3
    source.save(public)
    monkeypatch.setattr(public_memory, "_source_reader", source)
    stale.fire()  # Source changed before another turn had a chance to cancel it.
    assert not h.calls
    h.state["messages"].extend([
        AIMessage(content="PRIVATE-REPLY", tool_calls=[{"name": "lookup", "args": {"x": "PRIVATE-ARGS"}, "id": "p"}]),
        ToolMessage(content="PRIVATE-TOOL", tool_call_id="p"),
    ])
    h.state["incognito"] = False
    h.turn()
    stale.fire()
    h.timers[-1].fire()
    assert h.calls[-1]["previous_gist"] == ""
    assert h.card()["message_count"] == len(public)
    assert h.card()["tool_calls"] == []
    assert h.card()["final_output"] == "public answer"

    async def summarize(*args):
        return "PRIVATE-COMPACTED"

    graph = _FakeGraph(h.state["messages"])
    result = asyncio.run(compact_thread(
        graph, object(), None, _cfg(2), "chat-1", "chat-1", history_store=_FakeHistory(), summarizer=summarize,
    ))
    assert not result["refused"]
    h.state["messages"] = graph.updates[-1][1]["messages"][1:]
    # Reopen source and middleware, then run an ordinary turn over the compacted checkpoint.
    monkeypatch.setattr(public_memory, "_source_reader", PublicMemorySource(source.path))
    h.mw = memory.SessionSummaryMiddleware(config=h.config)
    h.mw.after_agent(h.state, None)
    assert "PRIVATE" not in json.dumps(h.card()) + json.dumps(h.calls)
    assert "rocket planning" in memory.load_digest(memory_dir=str(tmp_path)).block

    # Source changes while an auxiliary call is running, without a card rewrite.
    source.save(public * 2)
    h.mw.after_agent(h.state, None)
    before = h.card()

    def invoke(_):
        source.save(public * 3)
        return AIMessage(content="PRIVATE-STALE")

    monkeypatch.setattr(session_gist, "create_role_llm", lambda *_: SimpleNamespace(invoke=invoke))
    h.timers[-1].fire()
    assert h.card() == before
    assert memory.load_digest(memory_dir=str(tmp_path)).entries == []


# Helpers and scenario copied from tests/test_trace_export.py.

@dataclass
class _Outcome:
    context_id: str = "sess-1"
    task_id: str = "task-1"
    state: str = "completed"
    models: list = field(default_factory=lambda: ["claude-opus-4-8"])
    origin: str = "scheduler"
    trigger: str = "job-9"
    priority: str = ""
    cost_usd: float = 0.02
    duration_ms: int = 1500
    llm_calls: int = 2
    tool_calls: int = 1

_MESSAGES = [
    SystemMessage(content="You are a helpful agent."),
    HumanMessage(content="What's the weather?"),
    AIMessage(
        content="Let me check.",
        tool_calls=[{"name": "get_weather", "args": {"city": "SF"}, "id": "call_1"}],
    ),
    ToolMessage(content="72F and sunny", tool_call_id="call_1"),
    AIMessage(content="It's 72F and sunny in SF."),
]

@pytest.fixture(autouse=True)
def _reset():
    trace_export._reset_for_test()
    yield
    trace_export._reset_for_test()

def _enable(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_FLEET_TRACE_EXPORT", str(tmp_path))
    trace_export.init()
    assert trace_export.is_enabled()

def _read_rows(tmp_path):
    files = list(tmp_path.glob("fleet-traces-*.jsonl"))
    assert len(files) == 1, f"expected one daily dump, got {files}"
    return [json.loads(line) for line in files[0].read_text().splitlines()]

def _public_source(messages=_MESSAGES):
    from graph.public_memory import FleetExport, PublicSource

    return PublicSource("retained-version-2", tuple(messages), fleet=FleetExport(
        task_id="task-1", orient="Public plan",
        tools=[{"type": "function", "function": {"name": "public_tool", "parameters": {}}}],
        metadata={**vars(_Outcome()), "teacher": "public-agent", "trace_id": "public-trace", "thinking": "on"},
    ))

async def _src_export(tmp_path, monkeypatch, context_id):
    """Real private task storage survives restart and a later compacted public turn."""
    from a2a.server.context import ServerCallContext
    from a2a.types import a2a_pb2
    from langchain_core.messages import messages_from_dict, messages_to_dict

    from a2a_impl.stores import ReasoningCoalescingTaskStore, make_sqlite_engine
    from graph import public_memory
    from graph.compaction_engine import summarize_span, summary_message
    from runtime.state import STATE
    from server.a2a import _record_a2a_telemetry

    _enable(tmp_path, monkeypatch)
    operator = ServerCallContext(state={"principal_kind": "device"})
    db = str(tmp_path / "tasks.db")
    engine = make_sqlite_engine(db)
    store = ReasoningCoalescingTaskStore(engine)
    task = a2a_pb2.Task(id="task-1", context_id=context_id, history=[
        a2a_pb2.Message(role=a2a_pb2.ROLE_USER, parts=[a2a_pb2.Part(text="Public request")]),
    ])
    private = [HumanMessage(content="PRIVATE_STEERING_CANARY"), AIMessage(
        content="PRIVATE_REPLY_CANARY", tool_calls=[
            {"name": "private_tool", "args": {"text": "PRIVATE_ARGUMENT_CANARY"}, "id": "private-call"},
        ]), ToolMessage(content="PRIVATE_RESULT_CANARY", tool_call_id="private-call")]
    # Actually run the engine: its output retains the operator's private canary.
    private_summary = summarize_span(private, lambda prompt: "PRIVATE_COMPACTION_CANARY")
    assert "PRIVATE_STEERING_CANARY" in private_summary
    await store.save_private(task, {"messages": messages_to_dict(private), "summary": private_summary}, operator)
    await engine.dispose()

    engine = make_sqlite_engine(db)
    store = ReasoningCoalescingTaskStore(engine)
    try:
        record = await store.get_record(task.id, operator)
        retained_private = messages_from_dict(record.private_payload["messages"])
        assert retained_private[0].content == "PRIVATE_STEERING_CANARY"
        public_messages = [HumanMessage(content=m.parts[0].text) for m in record.task.history]
        prompts = []

        def summarize(prompt):
            prompts.append(prompt)
            return "Public request summary"

        compacted = summary_message(summarize_span(public_messages, summarize))
        assert prompts and all("PRIVATE_" not in prompt for prompt in prompts)
        task.history.append(a2a_pb2.Message(role=a2a_pb2.ROLE_AGENT, parts=[a2a_pb2.Part(text="Public reply")]))
        await store.save(task, operator)
        assert (await store.get_record(task.id, operator)).private_payload["summary"] == private_summary
        # Test-only trusted adapter: public SDK history and independently compacted
        # public summary become the shared reader's atomic public source.
        source = _public_source([compacted, *_MESSAGES])
        monkeypatch.setattr(public_memory, "_source_reader", lambda sid: source)
        monkeypatch.setenv("AGENT_NAME", "PRIVATE_TEACHER_CANARY")
        monkeypatch.setattr(trace_export, "_tool_schema_cache", [{"description": "PRIVATE_SCHEMA_CANARY"}])
        monkeypatch.setattr("observability.tracing.current_trace_id", lambda: "PRIVATE_TRACE_CANARY")
        monkeypatch.setattr("graph.goals.store.GoalStore", lambda: SimpleNamespace(read_plan=lambda sid: "PRIVATE_PLAN_CANARY"))
        monkeypatch.setattr("server.turn_telemetry.record_turn", lambda **kwargs: None)
        monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(thinking="PRIVATE_CONFIG_CANARY"))
        monkeypatch.setattr(STATE, "graph", SimpleNamespace(bound_tools=[]))
        outcome = SimpleNamespace(**{**vars(_Outcome(context_id=context_id)), "usage": {}, "incognito": True,
                                    "models": ["PRIVATE_MODEL_CANARY"], "trigger": "PRIVATE_TRIGGER_CANARY"})
        checkpoint = {"channel_values": {"incognito": True, "messages": retained_private}}
        monkeypatch.setattr(STATE, "checkpointer", SimpleNamespace(get_tuple=lambda config: SimpleNamespace(checkpoint=checkpoint)))
        _record_a2a_telemetry(outcome)
        assert not list(tmp_path.glob("*.jsonl"))
        checkpoint["channel_values"].update(incognito=False, messages=[summary_message(private_summary), *_MESSAGES])
        outcome.incognito = False
        _record_a2a_telemetry(outcome)
    finally:
        await engine.dispose()

    (row,) = _read_rows(tmp_path)
    exported = next(tmp_path.glob("*.jsonl")).read_bytes()
    assert b"PRIVATE_" not in exported
    assert b"Public request summary" in exported
    assert row["meta"]["session_id"] == context_id
    assert row["meta"]["orient"] == "Public plan"
    assert row["meta"]["model"] == "claude-opus-4-8"
    assert row["meta"]["cost_usd"] == 0.02
    assert row["tools"][0]["function"]["name"] == "public_tool"
    assert row["teacher"] == "public-agent"
    assert row["id"] == "fleet__public-trace"


# Helpers and scenario copied from tests/test_tracing.py.

def _reload_tracing():
    """Fresh module import of the real tracing.py so each test starts
    from init=disabled, even if a sibling test file inserted a stub
    into sys.modules first (test_exception_logging.py does this)."""
    import importlib.util
    from pathlib import Path

    import observability

    if "tracing" in sys.modules:
        del sys.modules["observability.tracing"]
    real_path = Path(__file__).parents[1] / "src" / "observability" / "tracing.py"
    spec = importlib.util.spec_from_file_location("observability.tracing", real_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["observability.tracing"] = module
    spec.loader.exec_module(module)
    # Keep the package attribute in lock-step with sys.modules. `from observability
    # import tracing` binds the attribute, while `import observability.tracing` and
    # `mock.patch("observability.tracing.<attr>")` resolve via sys.modules — if the
    # two diverge, a sibling test that patches an attribute patches one module object
    # while the code under test resolves the other (protoAgent#176 — the exact hazard
    # that breaks the audit-redaction suite's patch of tracing.trace_tool_call).
    observability.tracing = module
    return module

@pytest.fixture(autouse=True)
def _restore_tracing_module():
    """Each test here reloads a fresh tracing module (and may enable it with a fake
    Langfuse client) into sys.modules. Snapshot the ORIGINAL module object up front
    and put it back afterward — in BOTH sys.modules and the package attribute — so a
    sibling test file that captured `from observability import tracing` at import time
    keeps resolving the same, pristine (disabled) module (protoAgent#176)."""
    import observability

    orig_mod = sys.modules.get("observability.tracing")
    orig_attr = getattr(observability, "tracing", None)
    try:
        yield
    finally:
        if orig_mod is not None:
            sys.modules["observability.tracing"] = orig_mod
        if orig_attr is not None:
            observability.tracing = orig_attr

@pytest.fixture
def session_langfuse(monkeypatch):
    tracing = _reload_tracing()
    fake = MagicMock()
    span = MagicMock()
    span.trace_id = "c" * 32
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    return tracing, span

def _bind_private_source(monkeypatch, *, traceable=False):
    """Bind the dormant public-source reader so ``chat-private`` reads as a partitioned
    conversation (content traceable only once verified public) and everything else as an
    ordinary, unpartitioned conversation."""
    from langchain_core.messages import HumanMessage

    from graph import public_memory

    source = public_memory.PublicSource(
        version="retained-2", messages=(HumanMessage(content="Public request"),), traceable=traceable
    )
    monkeypatch.setattr(public_memory, "_source_reader", lambda sid: source if sid == "chat-private" else None)

async def _src_tracing(session_langfuse, monkeypatch):
    tracing, span = session_langfuse
    _bind_private_source(monkeypatch)

    # The private turn itself: input, tool args/result and the answer are all suppressed.
    async with tracing.trace_session("chat-private", name="chat", input="PRIVATE_STEER_CANARY"):
        assert tracing.io_allowed() is False
        tracing.trace_tool_call("lookup", {"q": "PRIVATE_ARG_CANARY"}, "PRIVATE_RESULT_CANARY", 5, True)
        tracing.set_session_output("PRIVATE_REPLY_CANARY")
    # A later ORDINARY turn (incognito=False) on the same partitioned conversation stays
    # suppressed — the partition can't be cleared to re-expose retained private state (#5403).
    async with tracing.trace_session("chat-private", name="chat", input="PRIVATE_LATER_CANARY", incognito=False):
        assert tracing.io_allowed() is False
        tracing.trace_tool_call("lookup", {"q": "PRIVATE_LATER_ARG"}, "PRIVATE_LATER_RESULT", 5, True)
        tracing.set_session_output("PRIVATE_LATER_REPLY")

    tool = tracing._langfuse.start_observation.call_args.kwargs
    assert (tool["input"], tool["output"]) == (None, None)
    # No canary reached ANY recorded span field (session input/output or tool args/result).
    blob = repr(span.update.call_args_list) + repr(tracing._langfuse.start_observation.call_args_list)
    assert "CANARY" not in blob


# Compaction helpers copied from tests/test_compaction_op.py.

class _FakeGraph:
    """Records aupdate_state calls; serves seeded messages from aget_state."""

    def __init__(self, messages, created_at=None):
        self._messages = messages
        self._created_at = created_at
        self.updates: list = []

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": list(self._messages)}, created_at=self._created_at)

    async def aupdate_state(self, config, update):
        self.updates.append((config, update))


class _FakeHistory:
    def __init__(self, *, yields=True):
        self.docs: list = []
        self._yields = yields

    def add(self, content, **kwargs):
        self.docs.append({"content": content, **kwargs})
        return 1 if self._yields else None


def _cfg(keep):
    return SimpleNamespace(compaction_keep_messages=keep)
