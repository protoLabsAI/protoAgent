# Cost & trace propagation

Two features that look similar but exist for different reasons: `cost-v1` emission and `a2a.trace` parsing.

## cost-v1 — outbound telemetry

### Why

A multi-agent fleet needs to answer: *how much is each (agent, skill) costing, and how often does it succeed?* Without this, every planner falls back to self-declared confidence — which agents can and do over-state.

cost-v1 is the measurement. The terminal artifact carries a URI-keyed metadata fragment with:

- `usage.input_tokens`, `usage.output_tokens`, `usage.total_tokens`
- `usage.cache_read_input_tokens`, `usage.cache_creation_input_tokens` — Anthropic-shaped prompt-cache tokens (ADR 0006)
- `durationMs`
- `costUsd` — the in-process estimate accumulated across the turn's LLM calls (`pricing.py`), plus any A2A peer's own reported `costUsd` taken verbatim (#3016); consumers prefer it over recomputing from tokens

The agent also **declares the extension** in its card (`capabilities.extensions`, URI `https://proto-labs.ai/a2a/ext/cost-v1`), which is what gates a consumer's cost interceptor.

A consuming system can keep a rolling window of samples per `(agent, skill)` key and use them to rank candidates for dispatch — replacing self-advertisement with observation after enough samples. See [ADR 0006](/adr/0006-observability-and-the-self-improving-flywheel).

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

The A2A executor (`a2a_impl/executor.py`) accumulates these `usage` frames across the run, then emits them (cache fields + the lifted-out `costUsd`) on the terminal artifact — since protolabs-a2a 0.3.0 as a **cost-v1 fragment in the artifact's `metadata` map keyed by the extension URI**, not as a DataPart. See [Extensions](/reference/extensions#cost-v1). For trace configuration and provider-specific coverage, see [Observability](/guides/observability). A LiteLLM gateway is not required for local cost telemetry.

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
  "method": "SendMessage",
  "params": {
    "message": {...},
    "metadata": {
      "a2a.trace": {
        "traceId": "0123456789abcdef0123456789abcdef",
        "spanId": "0123456789abcdef"
      }
    }
  }
}
```

Agent B validates the trace and span IDs and uses valid values to join the
caller's trace under the supplied parent. It also records caller IDs for
correlation. Invalid trace IDs start a fresh trace; invalid span IDs are ignored.
See [cross-agent propagation](/guides/observability#cross-agent-trace-propagation)
for setup and verification.

### Why not just use OpenTelemetry W3C traceparent

The convention carries trace context in A2A request metadata, so peers can
forward it with the request. It is a protoLabs convention, not a requirement of
A2A or a substitute for every HTTP tracing integration. Peers that ignore it
still process the task but start an independent trace.

## Why both ship in the template (and not as separate agents)

Both features are operational concerns, not agent-logic concerns. Every fork benefits from:

- Cost visibility without writing it per-agent
- Trace correlation without writing it per-agent

Putting both in the template means they're:

- Tested once (for every fork to benefit)
- Versioned atomically (an extension spec change updates all forks via the template's update path)
- Configurable through the [observability settings](/guides/observability); forks changing emitted wire behavior must also update their card declarations

The alternative — leaving each fork to implement them — guarantees most forks never will. Observability maintenance is the first thing skipped under deadline pressure.

## Related

- [Extensions reference](/reference/extensions) — wire shapes
- [Observability guide](/guides/observability) — how to consume these in production
- [A2A protocol](/explanation/a2a-protocol) — what the handler does around cost emission
