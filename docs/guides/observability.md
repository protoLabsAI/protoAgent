# Wire Langfuse + Prometheus

Use local telemetry for turn history, Langfuse for distributed traces,
Prometheus for monitoring, and the audit log for tool-call details. Start with a
configured agent and choose the signals you need; these layers work independently.

## Start with local evidence

Send one small test turn and inspect its row in **Telemetry**. Check the server
log for startup or connection failures and the [audit log](#audit-log) for tool
calls. These checks do not require Langfuse or Prometheus. If local evidence is
healthy and you need cross-agent traces or time-series monitoring, configure the
optional backends below and verify the same test turn reaches them.

## Local telemetry store

Enabled by default, `<instance_root>/telemetry.db` stores turn outcomes, tokens,
cost, duration, and model/tool-call counts, including coding-agent runs.
`telemetry.enabled: false` disables this store; `telemetry.db_path` relocates it.
History is pruned by `telemetry.retention_days` (default 90).

Use the operator token when querying a protected instance:

```bash
curl -H "Authorization: Bearer $PROTOAGENT_TOKEN" \
  http://localhost:7870/api/telemetry/summary
curl -H "Authorization: Bearer $PROTOAGENT_TOKEN" \
  'http://localhost:7870/api/telemetry/recent?limit=20'
curl -H "Authorization: Bearer $PROTOAGENT_TOKEN" \
  http://localhost:7870/api/telemetry/insights
```

`summary` includes totals, success rate, latency percentiles, cache-hit ratio,
and a per-model breakdown. `insights` flags outliers and possible savings; it
never changes configuration. Its reasons identify the baseline used: latency
compares like models or kinds of work, while cost compares priced turns.
`/api/telemetry/export` downloads CSV.

Plugin numeric series from `sdk.record_metric` live separately in `metrics.db`.
They remain enabled when turn telemetry is off because watches and other plugin
features depend on their history. See the
[consumption SDK](/guides/plugins#consumption-sdk).

## Langfuse (distributed traces)

In **Settings → Tracing**, enable **Send traces to Langfuse**, enter your host
and key pair, then restart the agent. This also works in a fleet member's
window. Keys are saved in untracked `secrets.yaml`. The non-secret config is:

```yaml
tracing:
  enabled: true
  host: https://cloud.langfuse.com
```

For containers, supply the complete environment key pair and destination:

```bash
LANGFUSE_PUBLIC_KEY=pk_lf_...
LANGFUSE_SECRET_KEY=sk_lf_...
LANGFUSE_HOST=https://langfuse.your-domain.com
```

| Credentials selected | Destination | Enablement |
| --- | --- | --- |
| Complete environment key pair | `LANGFUSE_HOST`, otherwise `http://host.docker.internal:3001` | Wins even over `tracing.enabled: false` |
| Config key pair | `tracing.host`, then `LANGFUSE_HOST`, then the same default | Requires `tracing.enabled: true` |

Environment keys never use the config's host. Specify a reachable destination;
the default is intended for a local Docker deployment. Changes require a
restart. Check the boot log for the selected layer and host, then send a test
turn and look for its trace in that project.

### What gets traced

- A2A turns use an `a2a-stream` root span, with model calls, tool calls, and
  subagent work nested beneath it.
- Tool observations include arguments, a result preview, and duration.
- ACP coding-agent runs use `acp:<delegate>` spans with the brief, reply or
  failure, plan, and tool spans.

Traces can contain conversation and tool content. Choose a destination suitable
for that data; incognito turns suppress content while retaining timing and usage.

### Coding-agent model calls (Claude Code)

When tracing is enabled, the Claude Code ACP integration sends native model-call
spans through a loopback OTLP relay to the selected Langfuse project. The relay
removes account identity attributes, redacts strings, maps token usage, and
associates spans with the correct turn. The coder receives a relay URL, not
Langfuse keys. Native prompt/tool-content logging flags are not enabled; the
agent-level traces described above can still contain content.

If model-call spans do not arrive within the fallback window, the relay records
the run's reported totals as a generation. Set `PROTOAGENT_ACP_NATIVE_TRACING=0`
to disable native coder tracing without disabling the agent's other traces.

### Cross-agent trace propagation

A2A callers can include `a2a.trace` in `params.metadata`:

```json
{
  "a2a.trace": {
    "traceId": "0123456789abcdef0123456789abcdef",
    "spanId": "0123456789abcdef"
  }
}
```

Valid 32-hex trace IDs join the caller's trace; valid 16-hex span IDs select its
parent span. Invalid trace IDs start fresh traces; invalid span IDs are ignored.
The caller IDs are also recorded as metadata for correlation.

<!-- docs-media: trace-correlation -->

<!-- /docs-media -->

### If Langfuse isn't configured

The agent continues without external traces. Authenticated
`GET /api/telemetry/recent` reports `tracing_enabled: false`; the console's Trace
column shows `off`. Check credentials, destination, and boot logs if tracing was
intended to be enabled.

## Prometheus metrics

Scrape `/metrics` on the agent's port. **A token-protected agent also protects
metrics**, unless `PROTOAGENT_PUBLIC_METRICS=1` explicitly makes them public.
Keep metrics on the monitoring network. Example
[Prometheus authorization configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#http_config):

```yaml
scrape_configs:
  - job_name: my-agent
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/protoagent-token
    static_configs:
      - targets: ['my-agent:7870']
```

Mount the token file in the Prometheus container and make the target resolvable
from that container. Core metric names use a sanitized `AGENT_NAME` prefix
(default `protoagent`). Useful series include `*_llm_calls_total`,
`*_llm_latency_seconds`, `*_llm_tokens_total`, `*_llm_cost_usd_total`,
`*_tool_calls_total`, and `*_active_sessions`. Inspect `/metrics` for the full
set and labels. Cost and cache usage depend on the model endpoint's reporting.

### If prometheus-client isn't installed

`/metrics` reports that metrics are disabled; the rest of the agent still runs.

## Fleet trace export (the flywheel Observe)

Fleet trace export writes conversation trajectories for downstream analysis or
training-data collection. It is **off by default**. Enable it per instance in
**Settings → Telemetry → Fleet trace export**, or in config:

```yaml
telemetry:
  fleet_trace_export: true
```

`PROTOAGENT_FLEET_TRACE_EXPORT` overrides the toggle: `1` enables, `0` disables,
and a directory path enables export at that path. The default is
`<instance_root>/fleet-traces/fleet-traces-YYYYMMDD.jsonl`.

Each row contains messages, tool calls/results and schemas, terminal outcome,
trace/session IDs, model, cost, and duration. Writes are best-effort and incognito
threads are excluded. These files contain raw conversation content.

### Shipping to a shared dataset (redaction)

The repository's collection scripts are lab deployment helpers; inspect their
paths and destinations before using them:

- `scripts/setup_fleet_tracing.sh` schedules transfer of **raw dumps** over SSH
  to a private collector (`ava` by default). Redaction happens on the collector,
  so raw data leaves the originating machine.
- `scripts/sync_fleet_traces.sh` redacts on the collector before copying into the
  dataset directory. By default it refuses to copy when the redactor is
  unavailable. `FLEET_REDACT=0` explicitly bypasses that protection.

The redactor combines secret patterns with a privacy model; output filenames
include the instance name. Review both the collected content and destination
access before allowing a rig to contribute.

The helpers scan a box-style `*/fleet-traces` layout and use their own
`PROTOAGENT_HOME` convention. Do not assume they discover desktop app data,
explicit instance roots, or Docker volumes automatically. For collector-side
extra directories, set `FLEET_TRACES_EXTRA_SOURCES`; confirm discovered paths
against `protoagent config explain`.

## Audit log

Tool calls handled by audit middleware append to
`<instance_root>/audit/audit.jsonl` (`/sandbox/audit/audit.jsonl` in the bundled
container). Rows include timestamp, session, tool, arguments, result summary,
duration, success, and trace ID for correlation with Langfuse.

Persist the entire `/sandbox` volume in Docker, including config and stores;
mounting only the audit directory does not preserve the agent's state. Audit
arguments and results can contain sensitive content.

### Forensic replay

Filter failed tool calls from the [Docker example](/guides/deploy-docker):

```bash
docker compose exec -T agent cat /sandbox/audit/audit.jsonl \
  | jq 'select(.success == false) | {ts, tool, result_summary}'
```

This inspects recorded results; it does not re-execute tools.

## Container logs

Logs go to standard error at `INFO` by default. Use `docker compose logs agent`
or `docker logs <container>` to inspect boot, delivery, and tool errors. Set
`LOG_LEVEL=DEBUG` for more detail or `LOG_FORMAT=json` for structured collection,
then recreate the container with the updated environment.

## Related

- [Cost and trace](/explanation/cost-and-trace)
- [Environment variables](/reference/environment-variables)
- [Back up and restore](/guides/backup-and-restore)
