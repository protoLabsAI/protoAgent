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
my_agent_llm_tools_deferred_total 128
my_agent_compactions_total 3
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

## Local telemetry store

Prometheus is live-scrape-only and Langfuse is opt-in/external — so protoAgent also keeps a **durable, queryable per-turn rollup** of its own (ADR 0006 Slice 2). One row is written per terminal A2A turn (completed / failed / canceled) with accumulated token usage (incl. prompt-cache), USD cost, wall-clock duration, and LLM-call + tool-call counts. This is the substrate for "what was expensive/slow over time" and the self-improving flywheel.

ON by default (one cheap write per turn). The SQLite path follows the usual `/sandbox` → `~/.protoagent` fallback and is instance-scoped (ADR 0004). Disable or relocate via config:

```yaml
telemetry:
  enabled: true             # default; false → no store, endpoints return {enabled:false}
  db_path: /sandbox/telemetry.db
```

Query it over HTTP (read-only):

```bash
# Aggregate rollup — totals, success rate, cache-hit ratio, p50/p95 latency, per-model split
curl localhost:7870/api/telemetry/summary
curl 'localhost:7870/api/telemetry/summary?since=2026-06-01T00:00:00+00:00'

# Most recent turns (newest first)
curl 'localhost:7870/api/telemetry/recent?limit=20'

# Insights (advise-only): flagged outlier turns + proven optimization levers
curl localhost:7870/api/telemetry/insights
```

`summary` returns `{turns, input_tokens, output_tokens, total_tokens, cache_read_input_tokens, cache_creation_input_tokens, cost_usd, llm_calls, tool_calls, avg_duration_ms, p50_duration_ms, p95_duration_ms, success_rate, cache_hit_ratio, by_model[]}`. `insights` flags turns ≥ 5× the rolling-median cost/latency and proves the cache lever (`{levers: {cache: {hit_ratio, est_savings_usd}, …}}`) — **advise-only**, no autonomous config changes. The operator console surfaces all of this under **Settings ▸ Overview** (Slices 3–4). History is pruned by `telemetry.retention_days` (default 90; `TelemetryStore.prune` runs on the maintenance loop), and `/api/telemetry/export` downloads it as CSV.

::: tip Plugin metric timeseries are a separate store
Named numeric series a plugin records (`sdk.record_metric` — treasury, net worth, fleet size, #1632) live in their own always-on per-instance `metrics.db`, **not** in this turn-rollup store: they're functional plugin state (history-dependent watch verifiers read them), so the `telemetry.enabled` toggle never affects them. See [Plugins ▸ consumption SDK](/guides/plugins#consumption-sdk).
:::

## Fleet trace export (the flywheel Observe)

The local stores above answer *"what did this agent cost?"*. **Fleet trace export** answers a different question — *"what does the agent actually do, turn by turn?"* — by writing one **trajectory** row per terminal turn in OpenAI chat format, so a fleet's real production traces can drive downstream training-data collection (the "Observe" step of the self-improving flywheel, ADR 0006 / #1897).

**Off by default.** Turn it on per instance either way:

```yaml
telemetry:
  fleet_trace_export: true    # Settings ▸ Telemetry ▸ "Fleet trace export"
```

```bash
# Env var — overrides the config toggle in BOTH directions:
PROTOAGENT_FLEET_TRACE_EXPORT=1              # on, default path
PROTOAGENT_FLEET_TRACE_EXPORT=0              # off (even if the toggle is on)
PROTOAGENT_FLEET_TRACE_EXPORT=/data/traces   # on, explicit path
```

On the desktop app, use the **Settings ▸ Telemetry** toggle — a GUI app doesn't inherit shell env, so the config toggle is the reachable path.

Each row lands in `<instance>/fleet-traces/fleet-traces-YYYYMMDD.jsonl` (append-only, daily-partitioned, instance-scoped) and carries:

```jsonc
{
  "id": "fleet__<trace_id>", "source": "protoagent-fleet", "teacher": "<AGENT_NAME>",
  "messages": [ /* OpenAI chat format, incl. tool_calls + tool results */ ],
  "tools":    [ /* the in-context OpenAI tool schemas */ ],
  "verified": true, "reward": 1.0,          // deterministic terminal-state outcome — never an LLM judge
  "meta": {
    "loop_shape": "ooda",                   // "ooda" when the goals subsystem was active, else "react"
    "orient": "<goal-plan snapshot>",       // the durable world-model artifact, when present
    "trace_id": "…", "session_id": "…", "model": "…", "cost_usd": 0.01, "duration_ms": 1500
  }
}
```

Writing is best-effort (never affects a turn) and honors the incognito gate — an incognito thread is never exported.

### Shipping to a shared dataset (redaction)

Raw dumps stay **on the machine**. To collect them centrally, `scripts/sync_fleet_traces.sh` redacts and forwards on a daily cron:

- **Hybrid redaction before the corpus** — deterministic regex (API keys / tokens / JWTs / emails / phones) **+** the `openai/privacy-filter` model (names, addresses, account numbers). Irreversible masking; structural fields (roles, tool names, ids, schemas) are untouched; `meta.redacted` is stamped. **Fail-closed** — it won't ship raw if the redactor is unavailable.
- Dest filenames are namespaced `<instance>__…` so many fleet members can't collide.

For a **laptop or desktop rig** off the collection box, `scripts/setup_fleet_tracing.sh` wires a once-daily rsync of that rig's raw dumps to the collection box over your private network (launchd on macOS, cron on Linux); the redaction boundary stays on the receiving box, so the rig needs no model or venv.

::: warning A personal rig's traffic is real data
Export is per-rig opt-in for a reason: a personal agent's turns contain real content. The redactor masks PII, but decide per rig whether it should contribute at all — the fleet containers are a clear yes; your personal desktop may be a no.
:::

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
