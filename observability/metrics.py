"""Prometheus metrics for protoAgent.

Exposes /metrics endpoint for scraping by Prometheus.
Falls back silently if prometheus-client is not installed.
Metric names derive from the AGENT_NAME env var so each fork gets its
own namespace without manual edits.
"""

from __future__ import annotations

import contextvars
import os
import re

_enabled = False
_llm_calls = None
_llm_latency = None
_llm_tokens = None
_llm_cache_tokens = None
_llm_cost = None
_tools_deferred = None
_compactions = None
_prunings = None
_tool_calls = None
_tool_latency = None
_active_sessions = None
_watch_fires = None
_watch_flapping = None
_a2a_turns = None
_a2a_turn_latency = None
_boot_phase_latency = None
_plugin_lifecycle_latency = None
_knowledge_op_latency = None

# Per-turn knowledge-op accumulator (#2676). The A2A executor arms it at turn
# start (``begin_knowledge_turn``); ``record_knowledge_op`` then folds each op's
# duration in, and the executor merges the result into the turn's telemetry row
# alongside the tool durations. A ContextVar so concurrent turns can't see each
# other's ops: child tasks/threads inherit a copy of the context that still
# references the SAME dict, so ops recorded inside tool bodies and recall
# middleware attribute to the arming turn. None ⇒ no turn armed (a background
# ingest, a CLI call) — the op still hits Prometheus, just not a turn row.
_knowledge_turn_ops: contextvars.ContextVar[dict[str, list[int]] | None] = contextvars.ContextVar(
    "_protoagent_knowledge_turn_ops",
    default=None,
)


def _prefix() -> str:
    raw = os.environ.get("AGENT_NAME", "protoagent")
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_") or "protoagent"


def init():
    global _enabled, _llm_calls, _llm_latency, _llm_tokens, _llm_cache_tokens, _llm_cost
    global _tools_deferred, _compactions, _prunings, _tool_calls, _tool_latency, _active_sessions
    global _a2a_turns, _a2a_turn_latency, _watch_fires, _watch_flapping, _boot_phase_latency
    global _plugin_lifecycle_latency, _knowledge_op_latency

    try:
        from prometheus_client import Counter, Histogram, Gauge

        p = _prefix()

        _llm_calls = Counter(
            f"{p}_llm_calls_total",
            "Total LLM API calls",
            ["model", "finish_reason"],
        )
        _llm_latency = Histogram(
            f"{p}_llm_latency_seconds",
            "LLM call latency",
            ["model"],
            buckets=[0.5, 1, 2, 5, 10, 20, 30, 60, 120],
        )
        _llm_tokens = Counter(
            f"{p}_llm_tokens_total",
            "Total LLM tokens consumed",
            ["model", "direction"],
        )
        _llm_cache_tokens = Counter(
            f"{p}_llm_cache_tokens_total",
            "Prompt-cache tokens (read vs creation)",
            ["model", "kind"],
        )
        _llm_cost = Counter(
            f"{p}_llm_cost_usd_total",
            "Estimated LLM cost in USD",
            ["model"],
        )
        _tools_deferred = Counter(
            f"{p}_llm_tools_deferred_total",
            "Tool schemas withheld from the model by deferral (ADR 0005/0006)",
        )
        _compactions = Counter(
            f"{p}_compactions_total",
            "History-compaction (summarization) events (ADR 0006)",
        )
        _prunings = Counter(
            f"{p}_tool_results_pruned_total",
            "Tool results pruned to head+tail stubs at context pressure (#2782, ADR 0101)",
        )
        _tool_calls = Counter(
            f"{p}_tool_calls_total",
            "Total tool executions",
            ["tool_name", "success"],
        )
        _tool_latency = Histogram(
            f"{p}_tool_latency_seconds",
            "Tool execution latency",
            ["tool_name"],
            buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30],
        )
        # Watch reactions (ADR 0067). Labelled by TRIGGER only — a watch id is
        # operator-defined and unbounded, so using it as a label is a cardinality blowup.
        # The watch id lives in the log line instead, where it's free.
        _watch_fires = Counter(
            f"{p}_watch_fires_total",
            "Watch reactions fired, by trigger (met | change)",
            ["trigger"],
        )
        # A repeating watch whose predicate oscillates (or whose monitored value moves every
        # poll) fires back-to-back. Each fire can enqueue an agent turn, so this is the signal
        # that a watch is costing far more than intended.
        _watch_flapping = Counter(
            f"{p}_watch_flapping_total",
            "Flapping episodes detected on a repeating watch (consecutive-fire runs)",
            ["trigger"],
        )
        _active_sessions = Gauge(
            f"{p}_active_sessions",
            "Active chat sessions",
        )
        # A2A turn outcomes — the durable signal for "turns are failing" alerting
        # straight off /metrics (state ∈ completed|failed|canceled|…). Low
        # cardinality. Paired latency histogram for turn duration.
        _a2a_turns = Counter(
            f"{p}_a2a_turns_total",
            "A2A turn outcomes by terminal state",
            ["state"],
        )
        _a2a_turn_latency = Histogram(
            f"{p}_a2a_turn_seconds",
            "A2A turn wall-clock duration",
            buckets=[0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300],
        )
        # Boot / session-init latency (#2674) — the one gap ADR 0006's turn-scoped
        # instrumentation structurally can't cover: nothing measures agent
        # construction itself, so "session felt slow to start" and "first turn was
        # slow" were indistinguishable. Labelled by the discrete builder phase
        # (`server.agent_init._init_langgraph_agent`) so a specific slow subsystem
        # (a heavy plugin, a large knowledge store) shows up by name.
        _boot_phase_latency = Histogram(
            f"{p}_boot_phase_seconds",
            "Agent construction / session-init latency by phase",
            ["phase"],
            buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60],
        )
        # Plugin lifecycle latency (#2675, instrumentation point #3 of #2245) —
        # per-plugin timing of the three lifecycle stages (load = module import,
        # config = schema/resolved-config binding, registration = register(registry)),
        # so a slow plugin shows up by NAME and stage instead of hiding inside the
        # aggregate "plugins" bucket of the boot-phase histogram above. `plugin` is
        # the installed-plugin id — operator-bounded (dozens at most), not a
        # cardinality risk like watch ids.
        _plugin_lifecycle_latency = Histogram(
            f"{p}_plugin_lifecycle_seconds",
            "Plugin lifecycle stage latency (load / config / registration), per plugin",
            ["plugin", "phase"],
            buckets=[0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
        )
        # Knowledge / memory-store op latency (#2676, instrumentation point #6 of
        # #2245) — the hybrid store's three hot paths: query (RRF hybrid search),
        # ingest (add_document / add_chunk), embed (embed_fn round-trips). `op` is
        # that closed three-value set, not a cardinality risk. Embed samples ride
        # INSIDE their enclosing query/ingest sample on purpose — the split shows
        # how much of a slow query was the embedding round-trip. Buckets span
        # sub-ms FTS5 lookups up to a transport-timeout embed outage.
        _knowledge_op_latency = Histogram(
            f"{p}_knowledge_op_seconds",
            "Knowledge-store operation latency (query / ingest / embed)",
            ["op"],
            buckets=[0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
        )
        _enabled = True
        print(f"[metrics] Prometheus metrics initialized (prefix={p}_)")
    except ImportError:
        print("[metrics] prometheus-client not installed. Metrics disabled.")


def is_enabled() -> bool:
    return _enabled


def record_llm_call(
    model: str,
    finish_reason: str,
    latency_s: float,
    tokens_input: int = 0,
    tokens_output: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    cost_usd: float = 0.0,
):
    """Record one LLM call (ADR 0006 Slice 1). Wired from the per-call seam in
    ``server._run_turn_stream`` — previously defined but never called."""
    if not _enabled:
        return
    _llm_calls.labels(model=model, finish_reason=finish_reason).inc()
    _llm_latency.labels(model=model).observe(latency_s)
    if tokens_input:
        _llm_tokens.labels(model=model, direction="input").inc(tokens_input)
    if tokens_output:
        _llm_tokens.labels(model=model, direction="output").inc(tokens_output)
    if cache_read:
        _llm_cache_tokens.labels(model=model, kind="read").inc(cache_read)
    if cache_creation:
        _llm_cache_tokens.labels(model=model, kind="creation").inc(cache_creation)
    if cost_usd:
        _llm_cost.labels(model=model).inc(cost_usd)


def record_tools_deferred(count: int):
    """Count tool schemas withheld from a model call by deferral (ADR 0006 Slice
    4b) — proves the tool-deferral lever is reducing the per-turn schema load."""
    if _enabled and _tools_deferred is not None and count > 0:
        _tools_deferred.inc(count)


def record_compaction():
    """Count a history-compaction event (ADR 0006) — proves the compaction lever
    fires + how often. Emitted from CountingSummarizationMiddleware."""
    if _enabled and _compactions is not None:
        _compactions.inc()


def record_pruning(results: int = 1):
    """Count tool results pruned to head+tail stubs (#2782, ADR 0101 D3) —
    proves the prune-before-summarize lever fires. Emitted from
    ToolResultPrunerMiddleware, one increment per rewritten result."""
    if _enabled and _prunings is not None:
        _prunings.inc(results)


def record_a2a_turn(state: str, duration_s: float | None = None):
    """Count one terminal A2A turn by ``state`` (completed/failed/canceled) and,
    when known, observe its wall-clock duration. Emitted from
    ``server/a2a.py::_record_a2a_telemetry`` so /metrics can alert on a failing
    or backed-up agent without scraping the telemetry SQL store."""
    if not _enabled:
        return
    if _a2a_turns is not None:
        _a2a_turns.labels(state=str(state or "unknown")).inc()
    if _a2a_turn_latency is not None and duration_s is not None and duration_s >= 0:
        _a2a_turn_latency.observe(duration_s)


def record_tool_call(tool_name: str, success: bool, latency_s: float):
    if not _enabled:
        return
    _tool_calls.labels(tool_name=tool_name, success=str(success)).inc()
    _tool_latency.labels(tool_name=tool_name).observe(latency_s)


def record_watch_fire(trigger: str, *, flapping: bool = False):
    """A watch fired its reaction. `flapping=True` additionally marks a consecutive-fire run —
    see WatchController's flap detection. No-ops when metrics are off."""
    if _watch_fires is not None:
        _watch_fires.labels(trigger=trigger or "met").inc()
    if flapping and _watch_flapping is not None:
        _watch_flapping.labels(trigger=trigger or "met").inc()


def record_boot_phase(phase: str, duration_s: float):
    """Record one agent-construction phase's wall-clock duration (#2674)."""
    if _enabled and _boot_phase_latency is not None:
        _boot_phase_latency.labels(phase=phase).observe(duration_s)


def record_plugin_lifecycle(plugin: str, phase: str, duration_s: float):
    """Record one plugin lifecycle stage's wall-clock duration (#2675). Emitted
    from ``graph.plugins.host.timed_lifecycle_phase`` around the loader's load /
    config / registration stages and pconfig's per-plugin schema discovery."""
    if _enabled and _plugin_lifecycle_latency is not None:
        _plugin_lifecycle_latency.labels(plugin=plugin, phase=phase).observe(duration_s)


def record_knowledge_op(op: str, duration_s: float):
    """Record one knowledge-store op's wall-clock duration (#2676). Emitted from
    ``knowledge.hybrid_store`` around the query / ingest / embed hot paths. Also
    folds the sample into the active per-turn accumulator when a turn armed one
    (``begin_knowledge_turn``) — that half works even with Prometheus disabled,
    since the turn row's durability doesn't depend on prometheus-client."""
    if _enabled and _knowledge_op_latency is not None:
        _knowledge_op_latency.labels(op=op).observe(duration_s)
    ops = _knowledge_turn_ops.get()
    if ops is not None:
        ops.setdefault(op, []).append(int(duration_s * 1000))


def begin_knowledge_turn() -> contextvars.Token:
    """Arm per-turn knowledge-op accumulation for the current context (#2676).
    Called by the A2A executor at turn start; disarm with ``end_knowledge_turn``."""
    return _knowledge_turn_ops.set({})


def current_knowledge_ops() -> dict[str, list[int]] | None:
    """The armed turn's accumulated ops (op → [ms, ...]), or None outside a turn."""
    return _knowledge_turn_ops.get()


def end_knowledge_turn(token: contextvars.Token) -> dict[str, list[int]]:
    """Disarm the per-turn accumulator and return what it captured."""
    ops = _knowledge_turn_ops.get() or {}
    try:
        _knowledge_turn_ops.reset(token)
    except ValueError:  # token minted in another context — just clear
        _knowledge_turn_ops.set(None)
    return ops


def session_started():
    if _enabled and _active_sessions:
        _active_sessions.inc()


def session_ended():
    if _enabled and _active_sessions:
        _active_sessions.dec()
