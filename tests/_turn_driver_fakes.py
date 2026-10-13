"""Shared fakes for the turn-driver characterization tests (epic #3804, phase A).

The seam is the GRAPH boundary: ``STATE.graph`` is a scripted stand-in whose
``astream_events`` replays one event script per call and whose ``ainvoke`` returns one
outcome per call. Everything between that boundary and the driver's public entry points
(``_chat_langgraph_stream`` / ``chat``) runs for real, so these tests survive a refactor
that moves code between functions or modules — they pin what comes OUT (frames, reply
dicts) and what reaches the boundary (graph inputs, configs, recorded state updates,
telemetry), never how the drivers are factored inside.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langgraph.types import Command

# ── scripted graph ────────────────────────────────────────────────────────────


def set_interrupt(value):
    """Script step: the graph parks at an ``interrupt(value)`` (ask_human / a form)."""

    def _step(graph):
        graph.pending.append(value)

    return _step


class Raise:
    """Script step / invoke outcome: raise ``exc`` at this point (mid-stream if events
    were already yielded)."""

    def __init__(self, exc: BaseException):
        self.exc = exc


class ScriptedGraph:
    """The graph surface both drivers touch.

    * ``astream_events`` pops one script per call: dict items are yielded as events, a
      ``Raise`` raises, a callable is run against the graph (e.g. ``set_interrupt``).
    * ``ainvoke`` pops one outcome per call: a ``Raise`` raises, a callable is run and its
      return value used, anything else is returned. ``usage`` outcomes fire the config's
      callbacks' ``on_llm_end`` so the non-streaming usage collector sees a model call.
    * interrupts are graph state: a ``Command(resume=…)`` input answers the first pending
      one; a fresh ``{"messages": …}`` input supersedes them all (a new run);
      ``aupdate_state(config, None)`` discards them all (the autonomous give-up).
    """

    def __init__(self, streams=(), invokes=()):
        self.streams = [list(s) for s in streams]
        self.invokes = list(invokes)
        self.pending: list = []
        self.stream_calls: list[tuple[Any, dict]] = []
        self.invoke_calls: list[tuple[Any, dict]] = []
        # Set when the driver makes a graph call the test didn't script — the drivers catch
        # Exception on these paths, so the raise alone could be swallowed into an error frame.
        self.overrun = False
        self.updates: list[tuple[dict, Any]] = []
        self.resumes: list = []
        self.on_call = None  # optional hook(graph, config) run at the start of each call

    def _answer_resume(self, graph_input):
        if isinstance(graph_input, Command):
            self.resumes.append(graph_input.resume)
            if self.pending:
                self.pending.pop(0)
        elif isinstance(graph_input, dict) and graph_input.get("messages"):
            # LangGraph: fresh input on a parked thread starts a new run — the stale
            # interrupt is superseded, not left pending behind the new turn's answer.
            self.pending.clear()

    async def astream_events(self, graph_input, config=None, version=None):
        self.stream_calls.append((graph_input, config))
        if self.on_call:
            self.on_call(self, config)
        self._answer_resume(graph_input)
        if not self.streams:
            self.overrun = True
            raise AssertionError(f"unscripted astream_events call #{len(self.stream_calls)}")
        script = self.streams.pop(0)
        for item in script:
            if isinstance(item, Raise):
                raise item.exc
            if callable(item):
                item(self)
                continue
            yield item

    async def ainvoke(self, graph_input, config=None):
        self.invoke_calls.append((graph_input, config))
        if self.on_call:
            self.on_call(self, config)
        self._answer_resume(graph_input)
        if not self.invokes:
            self.overrun = True
            raise AssertionError(f"unscripted ainvoke call #{len(self.invoke_calls)}")
        out = self.invokes.pop(0)
        if isinstance(out, Raise):
            raise out.exc
        if isinstance(out, Invoke):
            for step in out.steps:
                step(self)
            for model, tin, tout in out.usage:
                for cb in (config or {}).get("callbacks") or []:
                    cb.on_llm_end(_llm_result(model, tin, tout))
            if out.raises is not None:
                raise out.raises
            return out.result
        return out

    async def aget_state(self, config):
        interrupts = [SimpleNamespace(id=f"int-{i}", value=v) for i, v in enumerate(self.pending)]
        return SimpleNamespace(tasks=(), interrupts=interrupts)

    async def aupdate_state(self, config, values):
        self.updates.append((config, values))
        if values is None:
            self.pending.clear()


class Invoke:
    """An ``ainvoke`` outcome with side effects: ``steps`` run against the graph (e.g.
    ``set_interrupt``), ``usage`` = ``[(model, in, out), …]`` model calls reported to
    the config's callbacks, then ``raises`` (if set) is raised — a call that spent
    tokens and died."""

    def __init__(self, result=None, *, steps=(), usage=(), raises=None):
        self.result = result
        self.steps = list(steps)
        self.usage = list(usage)
        self.raises = raises


def _llm_result(model: str, tin: int, tout: int) -> LLMResult:
    msg = AIMessage(
        content="x",
        usage_metadata={"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout},
        response_metadata={"model_name": model},
    )
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


def turn_result(*msgs) -> dict:
    """An ``ainvoke`` result: the accumulated thread, THIS turn after the last Human."""
    return {
        "messages": [
            HumanMessage(content="earlier"),
            AIMessage(content="PREVIOUS ANSWER"),
            HumanMessage(content="q"),
            *msgs,
        ]
    }


# ── astream_events event builders ─────────────────────────────────────────────


def model_start(run: str | None) -> dict:
    return {"event": "on_chat_model_start", "name": "model", "run_id": run, "metadata": {}}


def chunk(run: str | None, msg, **metadata) -> dict:
    return {
        "event": "on_chat_model_stream",
        "name": "model",
        "run_id": run,
        "metadata": metadata,
        "data": {"chunk": msg},
    }


def text(run: str | None, s, **metadata) -> dict:
    return chunk(run, AIMessageChunk(content=s), **metadata)


def reasoning(run: str, s: str, **metadata) -> dict:
    return chunk(run, AIMessageChunk(content="", additional_kwargs={"reasoning_content": s}), **metadata)


def tool_chunk(run: str, tcid, name, **metadata) -> dict:
    return chunk(
        run,
        AIMessageChunk(content="", tool_call_chunks=[{"id": tcid, "name": name, "args": "", "index": 0}]),
        **metadata,
    )


def model_end(
    run, *, tool_calls=(), usage=None, model_name="resp-model", finish="stop", output=True, **metadata
) -> dict:
    if not output:
        return {"event": "on_chat_model_end", "name": "model", "run_id": run, "metadata": metadata, "data": {}}
    msg = AIMessage(
        content="",
        tool_calls=[{"id": tc[0], "name": tc[1], "args": tc[2]} for tc in tool_calls],
        response_metadata={"model_name": model_name, "finish_reason": finish},
    )
    if usage is not None:
        tin, tout, cread, ccreate = usage
        msg.usage_metadata = {
            "input_tokens": tin,
            "output_tokens": tout,
            "total_tokens": tin + tout,
            "input_token_details": {"cache_read": cread, "cache_creation": ccreate},
        }
    return {"event": "on_chat_model_end", "name": "model", "run_id": run, "metadata": metadata, "data": {"output": msg}}


def tool_start(run, name, inp=None, **metadata) -> dict:
    return {"event": "on_tool_start", "name": name, "run_id": run, "metadata": metadata, "data": {"input": inp or {}}}


def tool_end(run, name, output, **metadata) -> dict:
    return {"event": "on_tool_end", "name": name, "run_id": run, "metadata": metadata, "data": {"output": output}}


def tool_msg(content, tcid, *, status="success") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tcid, status=status)


def custom(name: str, data) -> dict:
    return {"event": "on_custom_event", "name": name, "run_id": "c", "metadata": {}, "data": data}


# ── goal controller ───────────────────────────────────────────────────────────


class FakeGoals:
    """A goal controller: ``decisions`` are returned by successive ``evaluate`` calls
    (``None`` once exhausted); ``("continue", note, message)`` or ``("done", note)``."""

    def __init__(self, decisions=(), *, iteration=0, fresh=False, forever=None):
        self.state = SimpleNamespace(iteration=iteration, fresh_context=fresh)
        self.decisions = list(decisions)
        self.forever = forever  # a decision tuple returned on every evaluate
        self.evals: list[str] = []
        self.kickoffs: list[str] = []
        self.active = True

    async def parse_control(self, message, session_id, *, trusted=True):
        return None  # not a /goal command

    def is_set_ack(self, reply):
        return False

    def active_goal(self, session_id):
        return self.state if self.active else None

    def kickoff_prompt(self, state, user_message=""):
        self.kickoffs.append(user_message)
        return f"KICKOFF<{user_message}>"

    async def evaluate(self, session_id, last_text=""):
        self.evals.append(last_text)
        d = self.forever or (self.decisions.pop(0) if self.decisions else None)
        if d is None:
            return None
        action, note, *rest = d
        self.state.iteration += 1
        return SimpleNamespace(action=action, note=note, message=(rest[0] if rest else ""), state=self.state)


# ── tracing spy ───────────────────────────────────────────────────────────────


class TraceSpy:
    def __init__(self):
        self.sessions: list[dict] = []
        self.outputs: list[str] = []
        self.flushes = 0

    def install(self, monkeypatch):
        from observability import tracing

        spy = self

        @contextlib.asynccontextmanager
        async def trace_session(session_id, name="agent-session", metadata=None, input=None, incognito=False):
            spy.sessions.append(
                {
                    "session_id": session_id,
                    "name": name,
                    "metadata": dict(metadata or {}),
                    "input": input,
                    "incognito": incognito,
                }
            )
            yield None

        def flush():
            spy.flushes += 1

        monkeypatch.setattr(tracing, "trace_session", trace_session)
        monkeypatch.setattr(tracing, "flush", flush)
        monkeypatch.setattr(tracing, "set_session_output", lambda out: spy.outputs.append(out))
        return self


class Clock:
    """A deterministic ``time`` stand-in for ``server.chat``: ``monotonic()`` is frozen
    until a script step ``clock.tick(s)`` advances it, so latencies are exact no matter
    how many times the driver reads the clock."""

    def __init__(self):
        import time as _time

        self.now = 1000.0
        self.time = _time.time

    def monotonic(self) -> float:
        return self.now

    def tick(self, seconds: float):
        def _step(_graph):
            self.now += seconds

        return _step
