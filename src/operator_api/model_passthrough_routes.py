"""Raw model passthrough — ``POST /api/models/{connection}/v1/chat/completions`` (#4608 part 1).

This is **NOT** the agent's own ``/v1/chat/completions``. The agent surface runs the whole
LangGraph turn — SOUL, memory, the tool loop, the ``<scratch_pad>``/``<output>`` protocol. This
one does none of that: it builds the model named by ``{connection}`` with the SAME builder the
graph uses (``graph.llm.create_llm`` on the qualified ``<connection>:<model>`` slot, ADR 0106) and
makes ONE plain OpenAI-shaped chat call against it. No graph, no tool execution, no persona.

Why it exists: evals speak OpenAI ``/chat/completions`` with ``tools`` (careercoach's
``evals/tool_selection/run_eval.py`` is the motivating one). A native subscription connection
(``anthropic-oauth`` / ``openai-codex``, ADR 0097) has no such endpoint of its own, so no agent
could eval a Claude-plan model. This route gives every registered connection — gateway, local
openai-compat, or native subscription — one uniform OpenAI endpoint, so an eval can point at a
subscription model exactly as it would at OpenAI.

The OpenAI wire shape and each client's accepted shape are NOT identical, so the route adapts
the request to the connection's registered TYPE (ADR 0106) — the one authoritative signal for
"what can this client take" — rather than forwarding every knob verbatim: ``tool_choice`` is
translated to Anthropic's native form for ``anthropic-oauth`` (``ChatAnthropic`` reads a bare
``"required"`` as a tool *name*), and a per-call knob a subscription backend rejects
(``temperature`` on Claude; ``temperature``/``max_tokens`` on Codex) is dropped so the model
defaults instead of 502-ing — see :data:`_PARAM_DROPS` and :func:`convert_tool_choice`.

Gating: it lives under ``/api``, so the operator bearer (``a2a_impl.auth``, default-deny) already
guards it like every other operator route. It is additionally loopback-only by default
(403 off 127.0.0.1/::1). Operators can enable ``model_passthrough.allow_remote`` to accept
remote callers; bearer auth still applies. The setting is read live on every request.

Usage: the call is billed through the same telemetry row the non-streaming chat turn writes — the
token counts come off a ``UsageMetadataCallbackHandler`` (the collector
``server/turn_telemetry.make_usage_callback`` builds for a chat turn), and the row carries the
subscription ``account_key``/``provider_id`` so spend shows in ``/api/usage`` (ADR 0122). The
collector and the row are mirrored here rather than imported because ``operator_api`` must never
import ``server`` (the import-layering contract in ``pyproject.toml``); the telemetry helpers live
in ``server`` only because they also publish on ``server._event_bus``, which this pure-model call
deliberately does not do.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
import uuid
from typing import Any

from fastapi import HTTPException, Request
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from runtime.state import STATE

log = logging.getLogger("protoagent.operator_api.model_passthrough")

# The row key prefix for a passthrough turn — the same convention
# ``server/turn_telemetry.local_task_id`` uses, so "which surface spent this" stays answerable.
_ORIGIN = "model_passthrough"

# Per-call OpenAI knobs each connection TYPE can actually accept. The OpenAI wire body
# carries ``temperature``/``max_tokens`` for everyone, but a native subscription client
# (ADR 0097) deliberately omits some because its backend rejects them — so forwarding the
# OpenAI value unchanged turns a plain eval into a 502 on exactly the connections this
# endpoint exists to serve. We MIRROR the builders rather than fight them:
#   * ``anthropic-oauth`` — ``build_anthropic_oauth_llm`` never sends ``temperature``; the
#     current Claude models (the 5 family and newer) reject it.
#   * ``openai-codex`` — the Codex Responses backend rejects ``max_output_tokens`` (what
#     langchain derives from ``max_tokens``) and non-default ``temperature`` on its
#     reasoning models; ``build_codex_llm`` sets neither.
# Omitted knobs fall back to the model's own default, which is the only value these
# subscription models will honour anyway.
_PARAM_DROPS: dict[str, frozenset[str]] = {
    "anthropic-oauth": frozenset({"temperature"}),
    "openai-codex": frozenset({"temperature", "max_tokens"}),
}


class ChatCompletionRequest(BaseModel):
    """The OpenAI ``/chat/completions`` request shape we accept. Extra OpenAI fields (``top_p``,
    ``n``, …) are ignored rather than rejected, so an eval's full body still goes through."""

    model: str = ""
    messages: list[dict] = []
    tools: list[dict] | None = None
    tool_choice: Any = None
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


# ── pure converters (no I/O, unit-testable) ────────────────────────────────────────────────


def _to_lc_tool_calls(raw: list[dict] | None) -> list[dict]:
    """OpenAI assistant ``tool_calls`` → LangChain ``AIMessage.tool_calls`` entries.

    OpenAI carries the arguments as a JSON STRING; LangChain wants a parsed dict. A malformed
    or empty arguments string becomes ``{}`` rather than raising — a passthrough must forward a
    prior turn's history as faithfully as it can, not reject it on the model's past formatting.
    """
    out: list[dict] = []
    for tc in raw or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, dict):
            parsed = args
        elif isinstance(args, str) and args.strip():
            try:
                parsed = json.loads(args)
            except (ValueError, TypeError):
                parsed = {}
        else:
            parsed = {}
        out.append(
            {
                "id": str(tc.get("id") or ""),
                "name": str(fn.get("name") or ""),
                "args": parsed if isinstance(parsed, dict) else {},
                "type": "tool_call",
            }
        )
    return out


def convert_tool_choice(tool_choice: Any, provider_type: str) -> Any:
    """Adapt an OpenAI ``tool_choice`` to what ``provider_type``'s client understands.

    OpenAI-compatible clients (gateway, local openai-compat, and the Codex Responses API,
    all built on ``ChatOpenAI``) take the OpenAI shape as-is, so it passes through.

    ``ChatAnthropic`` does NOT: its ``bind_tools`` treats any string other than
    ``"auto"``/``"any"`` as a *tool name* (so OpenAI's ``"required"`` would force a tool
    literally named ``required``), and it copies an OpenAI ``{"type":"function",…}`` dict
    through unchanged — both of which Anthropic rejects. For an ``anthropic-oauth``
    connection we translate to the Anthropic-native form:

    - ``"auto"`` → ``{"type":"auto"}``
    - ``"required"`` → ``{"type":"any"}`` (Anthropic's "must call some tool")
    - ``"none"`` → ``{"type":"none"}``
    - ``{"type":"function","function":{"name":"f"}}`` → ``{"type":"tool","name":"f"}``
    - a dict already in Anthropic shape, or a bare tool name, is passed through.
    """
    if tool_choice is None:
        return None
    if provider_type != "anthropic-oauth":
        return tool_choice
    if isinstance(tool_choice, str):
        low = tool_choice.strip().lower()
        if low in ("auto", "any", "none"):
            return {"type": low}
        if low == "required":
            return {"type": "any"}
        # A bare tool name — mirror ChatAnthropic's own string→tool mapping, explicitly.
        return {"type": "tool", "name": tool_choice}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            name = (tool_choice.get("function") or {}).get("name")
            return {"type": "tool", "name": str(name)} if name else None
        return tool_choice  # already Anthropic-native (auto/any/tool/none dict)
    return tool_choice


def openai_messages_to_langchain(messages: list[dict] | None) -> list[BaseMessage]:
    """OpenAI ``messages`` → LangChain messages.

    Handles ``system``/``user``/``assistant`` (including an assistant turn carrying
    ``tool_calls``) and the ``tool`` role (a tool result, keyed by ``tool_call_id``). An
    unrecognised role is carried as a human message so nothing in the eval's transcript is
    silently dropped.
    """
    out: list[BaseMessage] = []
    for m in messages or []:
        role = str(m.get("role") or "").strip().lower()
        content = m.get("content")
        if content is None:
            content = ""
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role in ("assistant", "ai"):
            tool_calls = _to_lc_tool_calls(m.get("tool_calls"))
            out.append(AIMessage(content=content or "", tool_calls=tool_calls) if tool_calls else AIMessage(content=content))
        elif role == "tool":
            out.append(ToolMessage(content=content, tool_call_id=str(m.get("tool_call_id") or "")))
        else:
            out.append(HumanMessage(content=content))
    return out


def _content_text(content: Any) -> str:
    """The assistant text as a plain string. OpenAI's ``content`` is a string; a LangChain
    message may hand back content blocks, so collapse a list of ``{type:"text", text}`` parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return "" if content is None else str(content)


def aimessage_to_openai_message(msg: AIMessage) -> dict:
    """A LangChain ``AIMessage`` → the OpenAI ``choices[0].message`` object.

    Tool calls come back out in OpenAI shape — ``{id, type:"function", function:{name, arguments}}``
    with ``arguments`` re-encoded to a JSON string — which is exactly what an eval that sent
    ``tools`` expects to parse."""
    tool_calls = []
    for tc in getattr(msg, "tool_calls", None) or []:
        name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
        tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
        tool_calls.append(
            {
                "id": str(tcid or f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {"name": str(name or ""), "arguments": json.dumps(args or {})},
            }
        )
    message: dict = {"role": "assistant", "content": _content_text(getattr(msg, "content", ""))}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def build_chat_completion(msg: AIMessage, *, model: str, usage: dict) -> dict:
    """Assemble the full OpenAI ``chat.completion`` response.

    ``finish_reason`` is ``tool_calls`` when the model asked to call a tool, else ``stop`` — the
    two outcomes an eval branches on."""
    message = aimessage_to_openai_message(msg)
    finish_reason = "tool_calls" if message.get("tool_calls") else "stop"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


# ── usage accounting (mirrors server/turn_telemetry; see module docstring for why) ──────────


def _make_usage_callback():
    """LangChain's per-model usage collector plus an ``llm_calls`` counter — the same object
    ``server/turn_telemetry.make_usage_callback`` attaches to a chat turn. Mirrored, not imported:
    ``operator_api`` may not import ``server``."""
    from langchain_core.callbacks import UsageMetadataCallbackHandler

    class _TurnUsageCallback(UsageMetadataCallbackHandler):
        def __init__(self) -> None:
            super().__init__()
            self.llm_calls = 0

        def on_llm_end(self, *args, **kwargs):
            self.llm_calls += 1
            return super().on_llm_end(*args, **kwargs)

    return _TurnUsageCallback()


def _openai_usage(per_model: dict[str, Any]) -> dict:
    """The per-model usage folded into the OpenAI wire ``usage`` shape, summed across every model
    call the turn made (mirrors ``server/turn_telemetry.sum_usage``)."""
    prompt = sum(int((u or {}).get("input_tokens", 0) or 0) for u in per_model.values())
    completion = sum(int((u or {}).get("output_tokens", 0) or 0) for u in per_model.values())
    total = sum(int((u or {}).get("total_tokens", 0) or 0) for u in per_model.values())
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total or (prompt + completion)}


def _fold_usage(per_model: dict[str, Any]) -> tuple[list[str], dict[str, int], float]:
    """``(models, summed usage, cost_usd)`` for the telemetry row — mirrors
    ``server/turn_telemetry.telemetry_usage``: cost is summed PER MODEL at its own rate, and
    ``input_tokens`` stays cache-inclusive here (the row writer splits it)."""
    from observability import pricing

    models = list(per_model or {})
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    cost = 0.0
    for model, u in (per_model or {}).items():
        u = u or {}
        details = u.get("input_token_details") or {}
        one = {
            "input_tokens": int(u.get("input_tokens", 0) or 0),
            "output_tokens": int(u.get("output_tokens", 0) or 0),
            "cache_read_input_tokens": int(details.get("cache_read", 0) or 0),
            "cache_creation_input_tokens": int(details.get("cache_creation", 0) or 0),
        }
        for k, v in one.items():
            totals[k] += v
        cost += pricing.cost_usd(model, one)
    return models, totals, round(cost, 6)


def _attribution(qualified: str) -> tuple[str, str, str]:
    """``(provider_id, account_key, bare_model)`` for the qualified slot, through the SAME
    resolution ``create_llm`` ran. A native subscription connection yields its account id (so
    ``/api/usage`` attributes the spend); an openai-compat gateway yields ``("", "")``."""
    from graph.delegation_usage import provider_account
    from graph.llm import resolve_slot_target

    _slot, _entry, ptype, bare = resolve_slot_target(qualified, STATE.graph_config)
    provider_id, account_key = provider_account(ptype)
    return provider_id, account_key, bare


def _record_usage(usage_cb, *, qualified: str, session_id: str, duration_ms: int) -> None:
    """Write one telemetry row for the passthrough call — the same store the chat turn's
    ``record_turn`` writes, so subscription spend lands in ``/api/usage``. Best-effort: a
    telemetry failure must never fail the model call.

    No ``turn.usage`` bus event is published (unlike a live chat turn): this is not a turn the
    fleet HUD's running-count pairs a ``turn.started`` with, so emitting the terminal half alone
    would corrupt that count — the same reason ``record_local_turn`` passes
    ``publish_usage_event=False``."""
    store = getattr(STATE, "telemetry_store", None)
    if store is None:
        return
    try:
        from datetime import datetime, timedelta, timezone

        per_model = getattr(usage_cb, "usage_metadata", None) or {}
        models, totals, cost = _fold_usage(per_model)
        provider_id, account_key, bare = _attribution(qualified)
        cache_read = totals["cache_read_input_tokens"]
        cache_creation = totals["cache_creation_input_tokens"]
        # Cache-EXCLUSIVE in the store (#3003): uncached + cache_read + cache_creation is the true
        # prompt size, each part billed at its own rate.
        input_tokens = max(0, totals["input_tokens"] - cache_read - cache_creation)
        output_tokens = totals["output_tokens"]
        ended = datetime.now(timezone.utc)
        created = ended - timedelta(milliseconds=max(0, duration_ms))
        row = {
            "task_id": f"{_ORIGIN}:{uuid.uuid4().hex[:12]}",
            "session_id": session_id,
            "state": "completed",
            "success": 1,
            "model": bare or (models[0] if models else ""),
            "models": ",".join(models),
            "provider_id": provider_id,
            "account_key": account_key,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + cache_read + cache_creation + output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "cost_usd": cost,
            "duration_ms": max(0, duration_ms),
            "llm_calls": int(getattr(usage_cb, "llm_calls", 0) or 0),
            "tool_calls": 0,
            "created_at": created.isoformat(),
            "ended_at": ended.isoformat(),
            "soul_rev": "",
            "trace_id": "",
            "tool_durations": None,
            "context_tokens": 0,
        }
        store.record(row)
    except Exception:  # noqa: BLE001 — usage recording must never break the model call
        log.debug("[model-passthrough] usage recording failed", exc_info=True)


# ── the gate ────────────────────────────────────────────────────────────────────────────────


def _is_loopback(host: str | None) -> bool:
    """True iff the TCP peer is this machine (127.0.0.0/8 / ::1 / localhost)."""
    h = (host or "").strip()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def register_model_passthrough_routes(app) -> None:
    @app.post("/api/models/{connection}/v1/chat/completions")
    async def _model_passthrough(connection: str, body: ChatCompletionRequest, request: Request):
        """A raw, non-streaming OpenAI chat call against one registered connection (ADR 0106).

        NOT the agent's ``/v1`` — no graph, no tools run, no SOUL, no memory. Just
        ``create_llm(<connection>:<model>).bind_tools(...).ainvoke(...)``, converted in and out
        of the OpenAI wire shape, so an eval can point at a subscription model (ADR 0097) the
        way it would point at OpenAI."""
        cfg = STATE.graph_config
        if not getattr(cfg, "model_passthrough_allow_remote", False) and not _is_loopback(
            request.client.host if request.client else None
        ):
            raise HTTPException(status_code=403, detail="model passthrough is loopback-only")
        if body.stream:
            raise HTTPException(status_code=400, detail="streaming is not supported; set stream:false")

        if cfg is None:
            raise HTTPException(status_code=503, detail="config not loaded")
        conn = (connection or "").strip().lower()
        entry = cfg.provider_by_id(conn)
        if entry is None:
            known = ", ".join(cfg.provider_ids()) or "(none registered)"
            raise HTTPException(status_code=404, detail=f"no model connection named {connection!r}; registered: {known}")
        # The connection's registered TYPE (ADR 0106) drives how the OpenAI-shaped per-call
        # params are adapted below — it is the one authoritative signal for "what can this
        # client actually take", resolved once so tool_choice and temperature/max_tokens stay
        # consistent instead of being patched knob by knob.
        ptype = (entry.type or "").strip().lower()
        if not (body.model or "").strip():
            # Without a model the qualified slot is just "<conn>:", and the gateway builder
            # falls back to the main agent's model name — sending one connection's model id to
            # another connection's endpoint. Name the model instead of guessing.
            raise HTTPException(
                status_code=400,
                detail="'model' is required: name the connection's model, e.g. {\"model\": \"claude-sonnet-4-6\", …}",
            )

        # The qualified-slot form IS how ADR 0106 names a connection's model — the same string a
        # model slot stores — so create_llm's registry dispatch builds the right client.
        qualified = f"{conn}:{body.model}"
        from graph.llm import create_llm

        try:
            llm = create_llm(cfg, model_name=qualified)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if body.tools:
            # OpenAI tool_choice must be translated for ChatAnthropic (it treats "required"
            # as a tool NAME and passes OpenAI's function dict through unchanged); the
            # openai-compat/Codex clients take it as-is. See convert_tool_choice.
            tool_choice = convert_tool_choice(body.tool_choice, ptype)
            tool_kw = {"tool_choice": tool_choice} if tool_choice is not None else {}
            llm = llm.bind_tools(body.tools, **tool_kw)

        # Forward only the per-call knobs this connection type can take (see _PARAM_DROPS):
        # a native subscription client rejects some, and this endpoint's main use case is
        # evals that set temperature against such a connection.
        drops = _PARAM_DROPS.get(ptype, frozenset())
        overrides: dict[str, Any] = {}
        if body.temperature is not None and "temperature" not in drops:
            overrides["temperature"] = body.temperature
        if body.max_tokens is not None and "max_tokens" not in drops:
            overrides["max_tokens"] = body.max_tokens
        dropped = [k for k in ("temperature", "max_tokens") if k in drops and getattr(body, k) is not None]
        if dropped:
            log.debug("[model-passthrough] %s does not accept %s; using the model default", ptype, ", ".join(dropped))

        lc_messages = openai_messages_to_langchain(body.messages)
        usage_cb = _make_usage_callback()
        started = time.monotonic()
        try:
            result = await llm.ainvoke(lc_messages, config={"callbacks": [usage_cb]}, **overrides)
        except Exception as exc:  # noqa: BLE001 — surface the model/gateway failure as a bad gateway
            raise HTTPException(status_code=502, detail=f"model call failed: {exc}") from exc
        duration_ms = int((time.monotonic() - started) * 1000)

        per_model = getattr(usage_cb, "usage_metadata", None) or {}
        _record_usage(usage_cb, qualified=qualified, session_id=f"{_ORIGIN}:{conn}", duration_ms=duration_ms)
        return build_chat_completion(result, model=body.model, usage=_openai_usage(per_model))
