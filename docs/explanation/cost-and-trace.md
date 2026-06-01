# Cost & trace propagation

Two features that look similar but exist for different reasons: `cost-v1` emission and `a2a.trace` parsing.

## cost-v1 — outbound telemetry

### Why

A multi-agent fleet needs to answer: *how much is each (agent, skill) costing, and how often does it succeed?* Without this, every planner falls back to self-declared confidence — which agents can and do over-state.

cost-v1 is the measurement. Every terminal task carries a DataPart with:

- `usage.input_tokens`, `usage.output_tokens`, `usage.total_tokens`
- `usage.cache_read_input_tokens`, `usage.cache_creation_input_tokens` — Anthropic-shaped prompt-cache tokens (ADR 0006), matching Workstacean's `CostArtifactUsage`
- `durationMs`
- `costUsd` — the in-process estimate accumulated across the turn's LLM calls (`pricing.py`); consumers prefer it over recomputing from tokens

The agent also **declares the extension** in its card (`capabilities.extensions`, URI `https://proto-labs.ai/a2a/ext/cost-v1`), which is what gates Workstacean's cost interceptor.

The consuming system (Workstacean's `defaultCostStore`) keeps a rolling window of samples per `(agent, skill)` key and uses them to rank candidates for dispatch — replacing self-advertisement with observation after 5+ samples. See [ADR 0006](/adr/0006-observability-and-the-self-improving-flywheel).

### Why token capture lives where it does

The temptation is to hook cost capture into the A2A handler. Don't — the handler has no visibility into individual LLM calls. A task may hit the model N times (tool-call loops, subagent delegation, retries). By the time the handler sees "task done", the per-call detail is gone.

The template captures in `_run_turn_stream` via the `on_chat_model_end` event from `astream_events(v2)`. The same seam reads the prompt-cache token details, measures per-call latency (paired with `on_chat_model_start`), resolves the model, computes `costUsd` (`pricing.py`), and — this is the part that was missing — feeds Prometheus via `metrics.record_llm_call` (ADR 0006). It then yields an enriched `usage` frame:

```python
elif kind == "on_chat_model_end":
    usage = getattr(output, "usage_metadata", None)
    details = usage.get("input_token_details") or {}
    usage_out = {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_input_tokens": int(details.get("cache_read", 0) or 0),
        "cache_creation_input_tokens": int(details.get("cache_creation", 0) or 0),
    }
    cost = pricing.cost_usd(model, usage_out)
    metrics.record_llm_call(model, finish_reason, latency_s, ..., cost_usd=cost)
    yield ("usage", {**usage_out, "cost_usd": cost})
```

The A2A handler accumulates these onto `TaskRecord.usage` during the run, then emits them (cache fields + the lifted-out `costUsd`) as a DataPart on the terminal artifact. Per-call **Langfuse** generation spans come from the LiteLLM gateway callback — protoAgent doesn't add a manual span that would bypass `trace_session` nesting.

### Why `stream_usage=True`

LangChain's streaming defaults to not populating `usage_metadata` on streaming chunks — the `AIMessageChunk` arrives with `usage_metadata=None`. Pass `stream_usage=True` to the `ChatOpenAI` client (which the template does in `graph/llm.py`) and `on_chat_model_end` gets the final chunk with usage populated.

Without that flag, cost-v1 emission silently produces empty payloads. The fix is one parameter; the symptom is "why is my cost dashboard showing zero tokens?" Easy to miss, costly when you do.

## a2a.trace — inbound trace context

### Why

When Agent A dispatches to Agent B via A2A, by default each gets its own Langfuse trace with no linkage. Debugging a failed multi-agent workflow then requires searching each agent's traces independently by timestamp — tedious and error-prone.

`a2a.trace` propagation makes the cross-agent link explicit.

### The convention

Agent A stamps its current Langfuse context into the outbound A2A request:

```json
{
  "method": "message/send",
  "params": {
    "message": {...},
    "metadata": {
      "a2a.trace": {
        "traceId": "abc123",
        "spanId": "def456"
      }
    }
  }
}
```

Agent B reads this in `a2a_handler.py`, opens its own trace, and stamps `caller_trace_id=abc123`, `caller_span_id=def456` into that trace's metadata.

An operator looking at Agent A's Langfuse trace can copy the trace ID and search Agent B (and C, D, …) by `metadata.caller_trace_id == abc123` to find every downstream trace that branched off.

### Why not just use OpenTelemetry W3C traceparent

Two reasons:

1. A2A is a protocol-level concept, not an HTTP-level one. Stamping into `params.metadata` survives re-serialization through SSE frames, MCP gateways, queue hops — anywhere OTel headers would get stripped.
2. Langfuse's span IDs don't map cleanly to W3C — there'd be a lossy translation layer. Using Langfuse-native IDs keeps the debug flow one-hop.

The trade-off is that this is a protoLabs convention, not an industry standard. Agents outside the fleet don't stamp the field, and the trace chain simply ends there. That's fine for our use case.

## Why both ship in the template (and not as separate agents)

Both features are operational concerns, not agent-logic concerns. Every fork benefits from:

- Cost visibility without writing it per-agent
- Trace correlation without writing it per-agent

Putting both in the template means they're:

- Tested once (for every fork to benefit)
- Versioned atomically (an extension spec change updates all forks via the template's update path)
- Cheap to strip if a specific fork doesn't want them (delete the `on_chat_model_end` handler and drop the extension from the card)

The alternative — leaving each fork to implement them — guarantees most forks never will. Observability maintenance is the first thing skipped under deadline pressure.

## Related

- [Extensions reference](/reference/extensions) — wire shapes
- [Observability guide](/guides/observability) — how to consume these in production
- [A2A protocol](/explanation/a2a-protocol) — what the handler does around cost emission
