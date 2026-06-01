"""Prometheus metrics for protoAgent.

Exposes /metrics endpoint for scraping by Prometheus.
Falls back silently if prometheus-client is not installed.
Metric names derive from the AGENT_NAME env var so each fork gets its
own namespace without manual edits.
"""

from __future__ import annotations

import os
import re

_enabled = False
_llm_calls = None
_llm_latency = None
_llm_tokens = None
_llm_cache_tokens = None
_llm_cost = None
_tool_calls = None
_tool_latency = None
_active_sessions = None


def _prefix() -> str:
    raw = os.environ.get("AGENT_NAME", "protoagent")
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_") or "protoagent"


def init():
    global _enabled, _llm_calls, _llm_latency, _llm_tokens, _llm_cache_tokens, _llm_cost
    global _tool_calls, _tool_latency, _active_sessions

    try:
        from prometheus_client import Counter, Histogram, Gauge

        p = _prefix()

        _llm_calls = Counter(
            f"{p}_llm_calls_total", "Total LLM API calls",
            ["model", "finish_reason"],
        )
        _llm_latency = Histogram(
            f"{p}_llm_latency_seconds", "LLM call latency",
            ["model"], buckets=[0.5, 1, 2, 5, 10, 20, 30, 60, 120],
        )
        _llm_tokens = Counter(
            f"{p}_llm_tokens_total", "Total LLM tokens consumed",
            ["model", "direction"],
        )
        _llm_cache_tokens = Counter(
            f"{p}_llm_cache_tokens_total", "Prompt-cache tokens (read vs creation)",
            ["model", "kind"],
        )
        _llm_cost = Counter(
            f"{p}_llm_cost_usd_total", "Estimated LLM cost in USD",
            ["model"],
        )
        _tool_calls = Counter(
            f"{p}_tool_calls_total", "Total tool executions",
            ["tool_name", "success"],
        )
        _tool_latency = Histogram(
            f"{p}_tool_latency_seconds", "Tool execution latency",
            ["tool_name"], buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30],
        )
        _active_sessions = Gauge(
            f"{p}_active_sessions", "Active chat sessions",
        )
        _enabled = True
        print(f"[metrics] Prometheus metrics initialized (prefix={p}_)")
    except ImportError:
        print("[metrics] prometheus-client not installed. Metrics disabled.")


def is_enabled() -> bool:
    return _enabled


def record_llm_call(model: str, finish_reason: str, latency_s: float,
                     tokens_input: int = 0, tokens_output: int = 0,
                     cache_read: int = 0, cache_creation: int = 0,
                     cost_usd: float = 0.0):
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


def record_tool_call(tool_name: str, success: bool, latency_s: float):
    if not _enabled:
        return
    _tool_calls.labels(tool_name=tool_name, success=str(success)).inc()
    _tool_latency.labels(tool_name=tool_name).observe(latency_s)


def session_started():
    if _enabled and _active_sessions:
        _active_sessions.inc()


def session_ended():
    if _enabled and _active_sessions:
        _active_sessions.dec()
