"""Fleet trace export — the Observe seam of the agent-fleet flywheel (#1897).

The lab (protoLab) mines the ProtoAgent Fleet's real production traces to find
where agents fail → which deterministic worlds to build → what to train. This
module emits those traces in the shape its training pipeline eats: one
**canonical Trajectory** JSON row per terminal A2A turn, OpenAI chat-format,
appended to a daily JSONL dump the lab ingests via ``dataset/adapters.py::_fleet``.

Schema is a three-repo contract (protoAgent = source, MythXEngine = env traces,
protoLab = consumer), pinned on protoAgent#1897. The row shape mirrors protoLab's
``experiments/agentic-data/dataset/schema.py``::

    id, source, teacher, domain, messages[], tools[], verified, reward,
    thinking, split, license_note, meta{...}

where ``meta`` carries the OODA signal the lab asked for — ``loop_shape``
(ReAct vs OODA, labelled by whether the goals subsystem was active) and
``orient`` (the durable goal-plan snapshot, our scratchpad-as-world-model).

Config
──────
Env-gated like Langfuse (``tracing.init``), off by default, graceful no-op:

    PROTOAGENT_FLEET_TRACE_EXPORT  unset / "0" / "off"  → disabled
                                   "1" / "on" / "true"  → enabled at the default
                                                          instance path
                                   <path>               → enabled, dumps there

Everything here is **best-effort**: a failure never touches the turn. The
terminal hook that calls ``export_turn`` already swallows exceptions, and each
helper degrades to a no-op / partial row rather than raising.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_enabled = False
_base_dir: Path | None = None

# Per-message content cap — a pathological turn (a giant tool dump) shouldn't
# produce a multi-MB row. The system prompt is well under this; genuine
# truncation is stamped into meta so the lab can filter.
_CONTENT_CAP = 100_000
# The goal-plan snapshot (our Orient artifact) is small by design; cap defensively.
_ORIENT_CAP = 16_000

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}

# OpenAI tool schemas, converted once from the graph's bound tools (static for
# the process lifetime) and reused across rows so the hot path stays cheap.
_tool_schema_cache: list[dict] | None = None


def init(config_enabled: bool = False) -> None:
    """Enable export from the env var and/or the config toggle. Idempotent.

    Enablement precedence — the env is the override in BOTH directions, the
    config toggle (``telemetry.fleet_trace_export``, surfaced in Settings so the
    desktop app can flip it) is the fallback:

    * ``PROTOAGENT_FLEET_TRACE_EXPORT`` = ``0``/``off``  → disabled (even if the
      config toggle is on)
    * = ``1``/truthy                                     → enabled, default path
    * = ``<path>``                                       → enabled, that path
    * unset, ``config_enabled=True``                     → enabled, default path
    * unset, ``config_enabled=False``                    → disabled

    Called after the config loads so ``config_enabled`` is known; env-only
    deployments (fleet containers, systemd) still work with no config.
    """
    global _enabled, _base_dir

    raw = os.environ.get("PROTOAGENT_FLEET_TRACE_EXPORT", "").strip()
    raw_l = raw.lower()
    env_set = raw != ""

    if env_set and raw_l in _FALSE:  # explicit off in the env — hard override
        print("[trace_export] fleet trace export disabled (env override).")
        return
    if not env_set and not config_enabled:
        print("[trace_export] fleet trace export disabled.")
        return

    try:
        if env_set and raw_l not in _TRUE:  # the env carried an explicit path
            _base_dir = Path(raw).expanduser()
        else:  # env truthy, or enabled via the config toggle → default location
            from infra.paths import instance_paths

            _base_dir = instance_paths().store("fleet-traces")
        _base_dir.mkdir(parents=True, exist_ok=True)
        _enabled = True
        src = "env" if env_set else "config"
        print(f"[trace_export] fleet trace export -> {_base_dir} (via {src})")
    except Exception as e:  # noqa: BLE001 — never fail boot for an export sink
        print(f"[trace_export] init failed: {e}. Export disabled.")


def is_enabled() -> bool:
    return _enabled


def _daily_path() -> Path:
    """Today's dump file — ``fleet-traces-YYYYMMDD.jsonl`` under the base dir."""
    assert _base_dir is not None
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _base_dir / f"fleet-traces-{day}.jsonl"


def _text_of(content: Any) -> str:
    """Flatten a LangChain message ``content`` to text.

    Content is usually a plain string; multimodal turns carry a list of parts
    (``{"type": "text", ...}`` / ``{"type": "image_url", ...}``). Keep the text,
    drop image bytes to a placeholder — the lab trains on interaction *shape*,
    not pixels."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text":
                    out.append(str(part.get("text", "")))
                elif part.get("type") in ("image_url", "image"):
                    out.append("[image]")
        return "\n".join(out)
    return str(content or "")


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _to_openai_messages(messages: list) -> tuple[list[dict], bool]:
    """LangChain checkpoint messages → OpenAI chat-format rows.

    Returns ``(messages, truncated)``. Roles map:
    Human→user, AI→assistant (+``tool_calls``), Tool→tool (+``tool_call_id``),
    System→system. ``tool_calls`` arguments are emitted as a JSON *string* to
    match the OpenAI wire shape the lab's adapter expects.
    """
    out: list[dict] = []
    truncated = False
    for m in messages:
        role = None
        # Prefer the class name (langchain), fall back to a ``.type`` attr.
        cls = type(m).__name__
        if cls == "HumanMessage":
            role = "user"
        elif cls == "AIMessage":
            role = "assistant"
        elif cls == "ToolMessage":
            role = "tool"
        elif cls == "SystemMessage":
            role = "system"
        else:
            mtype = getattr(m, "type", "")
            role = {"human": "user", "ai": "assistant", "tool": "tool", "system": "system"}.get(mtype)
        if role is None:
            continue

        content, cut = _cap(_text_of(getattr(m, "content", "")), _CONTENT_CAP)
        truncated = truncated or cut
        row: dict[str, Any] = {"role": role, "content": content}

        if role == "assistant":
            calls = []
            for tc in getattr(m, "tool_calls", None) or []:
                # langchain tool_calls: {name, args (dict), id}
                try:
                    args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                    tcid = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                    calls.append(
                        {
                            "id": tcid or "",
                            "name": name or "",
                            "arguments": json.dumps(args, default=str),
                        }
                    )
                except Exception:  # noqa: BLE001 — skip a malformed call, keep the turn
                    continue
            if calls:
                row["tool_calls"] = calls
        elif role == "tool":
            tcid = getattr(m, "tool_call_id", "") or ""
            if tcid:
                row["tool_call_id"] = tcid

        out.append(row)
    return out, truncated


def _tool_schemas(bound_tools: Any) -> list[dict]:
    """OpenAI tool schemas for the graph's bound tools (converted once, cached).

    ``bound_tools`` is ``STATE.graph.bound_tools`` — the full set bound to the
    lead agent. Deferred tools (progressive disclosure) narrow what's *visible*
    per turn; this is the superset, noted as such in ``meta``.
    """
    global _tool_schema_cache
    if _tool_schema_cache is not None:
        return _tool_schema_cache
    schemas: list[dict] = []
    try:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        for t in bound_tools or []:
            try:
                schemas.append(convert_to_openai_tool(t))
            except Exception:  # noqa: BLE001 — skip a tool that won't convert
                continue
    except Exception:  # noqa: BLE001 — no converter → empty tool set, not a crash
        schemas = []
    _tool_schema_cache = schemas
    return schemas


def _reward(state: str) -> tuple[bool, float | None]:
    """Verifiable outcome from the turn's terminal state (never an LLM judge).

    Per the locked #1897 contract: completed → verified success (1.0), failed →
    verified failure (0.0), anything else (canceled/unknown) → unverified/null.
    Dense subgoal-verifier rewards (``graph/goals/verifiers.py``) are a later,
    R3-scoped addition surfaced as ``meta.subgoal_events``.
    """
    if state == "completed":
        return True, 1.0
    if state == "failed":
        return True, 0.0
    return False, None


def _thinking_mode(graph_config: Any) -> str | None:
    """Map the configured thinking mode to the schema's on/off/null."""
    val = str(getattr(graph_config, "thinking", "") or "").lower()
    if val in ("enabled", "on", "true"):
        return "on"
    if val in ("disabled", "off", "false"):
        return "off"
    return None


def export_turn(outcome: Any, *, checkpointer: Any, graph_config: Any, bound_tools: Any = None) -> None:
    """Append one canonical Trajectory row for a terminal A2A turn.

    Called from the single terminal chokepoint (``_record_a2a_telemetry``),
    sync and best-effort — swallow everything. Reads the turn's messages from
    the checkpoint (sync ``get_tuple`` on the ``ThreadedSqliteSaver``), shapes
    them per the locked schema, and appends JSONL to today's dump.
    """
    if not _enabled or _base_dir is None:
        return
    try:
        session_id = getattr(outcome, "context_id", "") or ""
        task_id = getattr(outcome, "task_id", "") or ""
        state = getattr(outcome, "state", "") or ""

        # Messages live in the checkpoint, keyed by the resolver's thread_id.
        # The template default is ``a2a:<session_id>`` (server/chat._resolve…);
        # a fork with a custom resolver scopes elsewhere — v1 targets the
        # default, which is what the dev/prod template instances use.
        messages: list = []
        incognito = False
        if checkpointer is not None and session_id:
            try:
                tup = checkpointer.get_tuple({"configurable": {"thread_id": f"a2a:{session_id}"}})
                channel_values = (getattr(tup, "checkpoint", None) or {}).get("channel_values", {}) if tup else {}
                incognito = bool(channel_values.get("incognito"))
                messages = channel_values.get("messages", []) or []
            except Exception:  # noqa: BLE001 — no checkpoint → skip, don't crash the hook
                messages = []

        # Incognito threads (ADR 0069 D3b) leave no memory trail — honour that
        # here too; never export a turn the user asked to be forgotten.
        if incognito or not messages:
            return

        oai_messages, truncated = _to_openai_messages(messages)
        if not oai_messages:
            return

        # Orient artifact: the durable goal-plan snapshot. Its presence is our
        # ReAct-vs-OODA label (goals subsystem active ⇒ OODA/Orient turn).
        orient = ""
        try:
            from graph.goals.store import GoalStore

            orient = (GoalStore().read_plan(session_id) or "").strip()
        except Exception:  # noqa: BLE001 — plan is optional
            orient = ""
        orient, _ = _cap(orient, _ORIENT_CAP)
        loop_shape = "ooda" if orient else "react"

        verified, reward = _reward(state)
        models = list(getattr(outcome, "models", None) or [])

        # Trace id ties this row to the distributed Langfuse trace (and to any
        # cross-agent delegation rows that share it). Best-effort — the trace
        # contextvar is still set inside the terminal hook's scope.
        trace_id = ""
        try:
            from observability import tracing

            trace_id = tracing.current_trace_id() or ""
        except Exception:  # noqa: BLE001
            trace_id = ""
        row_id = f"fleet__{trace_id or task_id}"

        row = {
            "id": row_id,
            "source": "protoagent-fleet",
            "teacher": os.environ.get("AGENT_NAME", "protoagent"),
            "domain": "unknown",  # skill/domain inference is a follow-up
            "messages": oai_messages,
            "tools": _tool_schemas(bound_tools),
            "verified": verified,
            "reward": reward,
            "thinking": _thinking_mode(graph_config),
            "split": "train",  # fleet reality is always train-split
            "license_note": None,
            "meta": {
                "trace_id": trace_id,
                "session_id": session_id,
                "task_id": task_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": models[0] if models else "",
                "models": models,
                "origin": getattr(outcome, "origin", "") or "",
                "trigger": getattr(outcome, "trigger", "") or "",
                "priority": getattr(outcome, "priority", "") or "",
                "loop_shape": loop_shape,
                "orient": orient,
                "outcome_state": state,
                "cost_usd": float(getattr(outcome, "cost_usd", 0.0) or 0.0),
                "duration_ms": int(getattr(outcome, "duration_ms", 0) or 0),
                "llm_calls": int(getattr(outcome, "llm_calls", 0) or 0),
                "tool_calls": int(getattr(outcome, "tool_calls", 0) or 0),
                # v1 caveats for the lab's adapter:
                "delegation": None,  # in-process task() subagents are embedded in messages;
                                     # cross-agent A2A delegations link via shared trace_id
                "tools_note": "bound superset; deferred-tool visibility narrows per turn",
                "reward_semantics": "terminal-state verifier (completed=1.0, failed=0.0); "
                                    "dense subgoal-verifier events are R3 follow-up",
                "content_truncated": truncated,
            },
        }

        with _daily_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 — export must never affect a turn
        log.exception("[trace_export] failed to export turn %s", getattr(outcome, "task_id", "?"))


def _reset_for_test() -> None:
    """Test hook — clear module state between cases."""
    global _enabled, _base_dir, _tool_schema_cache
    _enabled = False
    _base_dir = None
    _tool_schema_cache = None
