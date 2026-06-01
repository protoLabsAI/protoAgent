# Wire Langfuse + Prometheus

The template has three independent observability layers: Langfuse traces, Prometheus metrics, and a JSONL audit log. This guide lights up all three.

## Langfuse (distributed traces)

Set these env vars on the running container:

```bash
LANGFUSE_PUBLIC_KEY=pk_lf_...
LANGFUSE_SECRET_KEY=sk_lf_...
LANGFUSE_HOST=https://langfuse.your-domain.com   # or http://host.docker.internal:3001 for local
```

That's it. `tracing.init()` runs at server boot, detects the keys, and connects. Traces show up tagged with `AGENT_NAME`.

### What gets traced

- Each A2A task → a root span named `a2a.task`
- Each LangGraph run → a child span with tool calls + LLM calls nested beneath
- Each subagent delegation → a nested span under the parent's
- Each tool call → a `tool:<name>` observation with args + result preview + duration

### Cross-agent trace propagation

If the caller stamps its trace context into `params.metadata["a2a.trace"]`:

```json
{
  "jsonrpc": "2.0",
  "method": "message/send",
  "params": {
    "message": { "role": "user", "parts": [...] },
    "metadata": {
      "a2a.trace": {
        "traceId": "abc123",
        "spanId": "def456"
      }
    }
  }
}
```

...this agent's trace gets `caller_trace_id=abc123` stamped in its metadata. Filter Langfuse by that field to find every agent trace spawned from one dispatch.

### If Langfuse isn't configured

Every tracing helper becomes a no-op. No crashes, no latency. The rest of the agent doesn't care whether tracing is on.

## Prometheus metrics

Scrape `/metrics` on port 7870. Metric names are prefixed with a sanitized `AGENT_NAME`.

```
my_agent_llm_calls_total{model="claude-opus-4-8",finish_reason="stop"} 42
my_agent_llm_latency_seconds_bucket{model="claude-opus-4-8",le="5"} 38
my_agent_llm_tokens_total{model="claude-opus-4-8",direction="input"} 184320
my_agent_llm_cache_tokens_total{model="claude-opus-4-8",kind="read"} 96000
my_agent_llm_cost_usd_total{model="claude-opus-4-8"} 0.83
my_agent_tool_calls_total{tool_name="web_search",success="True"} 17
my_agent_active_sessions 3
```

The LLM series (`*_llm_calls_total`, `*_llm_latency_seconds`, `*_llm_tokens_total`, `*_llm_cache_tokens_total`, `*_llm_cost_usd_total`) are emitted per LLM call from `server._run_turn_stream` (ADR 0006); cache + cost are best-effort and depend on the gateway surfacing prompt-cache token details. Tool series come from `AuditMiddleware`.

Example Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: my-agent
    static_configs:
      - targets: ['my-agent:7870']
```

### If prometheus-client isn't installed

`/metrics` returns a note saying metrics are disabled. No other side effects.

## Audit log

Every tool call is written to `/sandbox/audit/audit.jsonl`. One line per call:

```json
{
  "ts": "2026-04-17T13:23:42.644606+00:00",
  "session_id": "s-abc",
  "tool": "web_search",
  "args": {"query": "protoLabs AI agent"},
  "result_summary": "2 result(s) for 'protoLabs AI agent':\\n1. ...",
  "duration_ms": 842,
  "success": true,
  "trace_id": "abc123"
}
```

Mount `/sandbox/audit/` as a volume if you want the log to persist across container restarts. The trace_id lets you jump from a JSONL line straight into the Langfuse UI for the full run.

### Forensic replay

```bash
docker exec my-agent cat /sandbox/audit/audit.jsonl \
    | jq 'select(.success == false) | {ts, tool, result_summary}'
```

## Container logs

The template calls `logging.basicConfig(level=INFO)` at module import, which is above Python's default WARNING threshold. Webhook delivery events, session boundaries, and tool errors all land in `docker logs`.

To go louder for debugging:

```bash
docker run -e LOG_LEVEL=DEBUG ...
```

## Related

- [Explanation: cost and trace](/explanation/cost-and-trace) — why distributed tracing looks this specific way
- [Environment variables reference](/reference/environment-variables) — every knob
