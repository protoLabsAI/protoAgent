# Operate the model in-flight limiter

Nothing in protoAgent bounds how many chat-model calls a process has in flight at
once, so a burst of fan-outs all hit the gateway together, all time out together, and
all retry together — the #209 amplification. The **in-flight limiter** ([ADR 0115](../adr/0115-gateway-inflight-limiter.md))
puts extra callers into a bounded, priority-ordered local queue per lane instead of
letting them send requests that are bound to time out. It is **off by default**
(`model.max_inflight: 0`, zero overhead beyond one branch); this guide is how you turn
it on, size it, and read what it's doing.

A **lane** is one `(resolved base_url, model id)` pair — the same key your
[LiteLLM gateway](../explanation/litellm-gateway.md) connections resolve to, plus
`anthropic-oauth|<model>` and the codex endpoint for those providers. Every lane in a
process shares one `max_inflight`.

## What it covers — and what it can't

The limiter is **per-process**. It bounds every chat-model call this process makes
through `create_llm`: the gateway client, its codex subclass, and anthropic-oauth. It
does **not** see, and cannot bound:

- **ACP coder delegates** (claude-code, proto/protoCLI, opus/sonnet) — separate
  processes that call their endpoints with their own clients.
- **pr-reviewer's clawpatch** — a subprocess of the reviewer plugin.
- **Other protoAgent instances on the same box** — each has its own budget.

Those callers are bounded only by their own concurrency settings (a board's
`max_concurrent`, pr-reviewer's `max_concurrent_panels`, protoPatch's time budget) and
by **gateway-side limits** — LiteLLM per-key / per-deployment parallel-request / rpm /
tpm caps. Because the limiter can't reach across processes, **gateway-side limits are
the backstop**: a local limit sized below the gateway's real capacity keeps 429s rare,
but only the gateway sees every process and every coder at once. Embeddings (their own
8 s timeout, no retries), raw `gateway_client` httpx calls (images, transcription), and
`acp:` aux models never go through a lane either.

## Turn it on when the fleet retries in sync

The signal that you need this is the #209 symptom: under load, a member's log fills with
**synchronized `openai._base_client Retrying request` bursts** and request timeouts,
recurring every couple of minutes across the fleet, with no review or turn finishing.
The gateway is accepting connections but not answering in time, so nobody queues —
every caller times out and retries together, and each retry re-sends work the gateway
may still be chewing on. That is the wave the limiter replaces with visible queuing.

Enable it per instance in `langgraph-config.yaml` (or **Settings ▸ Model & runtime**;
the keys are hot-reloadable):

```yaml
model:
  max_inflight: 6                 # 0 (default) = limiter off; per-lane, this process
  inflight_queue_timeout: 300     # seconds a caller may wait for a slot (separate from request_timeout)
  inflight_interactive_reserve: 1 # slots only interactive callers may take
```

A config change applies to acquisitions made **after** the reload; slots already held
are never revoked. Setting `max_inflight` back to `0` drains the queue and returns to
today's unlimited behaviour.

## Size the limit

Keep each process's `max_inflight` **below the gateway's real parallel capacity** for
that lane — the number of concurrent requests the gateway (and the provider behind it)
actually serves without slowing down, not its rpm/tpm ceiling. Set it at or above that
number and you've queued nothing that would otherwise have timed out; a limit set *too
low* throttles throughput the gateway could have served, which is why the default is
off rather than a guessed number.

Because the budget is **per-process**, add the budgets up across every instance that
shares a lane and keep the **total within the gateway's capacity**. Two instances at
`max_inflight: 6` each put 12 calls on a gateway sized for 8 — each stays under its own
limit while together they oversubscribe it. Size them together, and let the gateway-side
limit catch what's left.

## Priority classes and the interactive reserve

When a lane is saturated, waiters are served strictly by class, then first-come-first-served
within a class:

| Class | Who runs under it |
|---|---|
| `interactive` | Chat or console turns an operator is watching (`server/chat.py` tags these). |
| `default` | A2A, background and scheduled turns, subagents — anything untagged. |
| `bulk` | Workflow fan-outs, sweeps, review-panel finders (`plugins/workflows/engine.py` tags recipe fan-outs). |

Two rules keep this from starving or locking anyone out:

- **Aging.** A waiter's effective class rises one step for every 60 s it has waited (an
  internal constant, not a knob), so a steady stream of `default`/`interactive` work can
  never starve queued `bulk` work forever.
- **Reserve.** `inflight_interactive_reserve` slots are handed only to `interactive`
  waiters (clamped to `max_inflight − 1`, so non-interactive work always keeps at least
  one slot). A panel burst can't take the operator's last slot from their chat.

The class rides a `contextvars.ContextVar`, so a subagent `task()` inherits its parent's
class. A plugin running its own burst of model work marks it explicitly with
`sdk.llm_priority()`:

```python
from graph import sdk

async with sdk.llm_priority("bulk"):        # or "with"; the class is inherited by tasks
    await asyncio.gather(*(run_finder(f) for f in finders))
```

`sdk.llm_priority` rejects any class outside `interactive` / `default` / `bulk`, and
costs nothing when the limiter is off.

## Queue timeout vs request timeout

The slot is acquired **outside** the HTTP request and the per-chunk stall guard, so
**queue wait never counts toward `request_timeout`** — `request_timeout` goes back to
meaning backend latency once a slot is held. Queue wait has its own separate bound,
`inflight_queue_timeout` (default 300 s, kept below `turn_stall_timeout_seconds` so a
queued turn fails with a queue error, not a stall).

When a caller waits past `inflight_queue_timeout`, it gets **`GatewayQueueTimeout`** (a
`TimeoutError` subclass). The message names the lane, the wait, and the queue position:

```
gateway lane 'https://gw/v1|protolabs/smart' in-flight queue timed out after 300.0s
at queue position 14 — raise model.max_inflight or model.inflight_queue_timeout, or
reduce concurrent load on this lane
```

It is deliberately **not retried** — retrying a queue timeout just rejoins the queue and
reproduces the #209 amplification locally — and, because acquisition sits outside the
stall guard, it is never mislabelled a `StreamStallTimeout`. When you see one, pick the
fix the message points at:

- **Raise `model.max_inflight`** — only if the gateway has headroom (slots were full but
  the gateway itself was answering; see below). This serves more concurrently.
- **Raise `model.inflight_queue_timeout`** — if the queue is moving but a deep backlog
  needs longer than 300 s to clear, and the turn can afford the wait.
- **Reduce bulk fan-out** — fewer concurrent finders / a smaller sweep upstream, so the
  lane isn't oversubscribed in the first place.

## Read the lane metrics

Two read-only surfaces expose the same per-lane state; both read memory only.

**Prometheus.** Four series on `/metrics`, prefixed with your sanitized `AGENT_NAME`
(shown here as `my_agent_`):

```
my_agent_llm_inflight{lane="https://gw/v1|protolabs/smart"} 6
my_agent_llm_queue_depth{lane="https://gw/v1|protolabs/smart",priority="bulk"} 7
my_agent_llm_queue_wait_seconds_bucket{lane="…",priority="bulk",le="120"} 41
my_agent_llm_queue_timeouts_total{lane="https://gw/v1|protolabs/smart"} 2
```

- `*_llm_inflight{lane}` (gauge) — calls currently holding a slot.
- `*_llm_queue_depth{lane,priority}` (gauge) — calls waiting, split by class.
- `*_llm_queue_wait_seconds{lane,priority}` (histogram; buckets 0.1, 0.5, 1, 5, 15, 30,
  60, 120, 300) — how long a call waited before it was granted or timed out.
- `*_llm_queue_timeouts_total{lane}` (counter) — calls that gave up (`GatewayQueueTimeout`).

**HTTP snapshot.** `GET /api/telemetry/llm-lanes` (operator-authenticated, same tier as
the rest of `/api/telemetry/*`) returns the live snapshot straight from memory — no
network or DB call, cheap enough to poll every tick:

```json
{"enabled": true, "generated_at": "2026-09-28T17:04:11+00:00",
 "lanes": [{"lane": "https://gw/v1|protolabs/smart", "limit": 6, "reserve": 1,
            "inflight": 6, "queued": 9,
            "queued_by_priority": {"interactive": 0, "default": 2, "bulk": 7},
            "oldest_wait_s": 212.4, "wait_p50_s_5m": 38.0, "wait_p90_s_5m": 171.0,
            "queue_timeouts_5m": 2, "saturated": true}]}
```

`saturated` is true when `queued > 0` has held continuously for 60 s or more; the
percentiles and `queue_timeouts_5m` are computed over a rolling 5-minute window. When the
limiter is off the route returns `{"enabled": false}`. A plugin running *inside* the
agent process (pr-reviewer's panels, say) reads the same payload with no round-trip via
`sdk.llm_lanes()`.

## Tell local queuing from a slow gateway

The whole point of the metrics is to separate two failure modes the #209 wave used to
blur together:

- **`local_queue`** — `saturated: true`, or `wait_p90_s_5m` above your threshold, **with
  slots full**. The gate is slow because *this process is queuing on purpose*. This is
  the limiter working; base ETAs on queue depth, and only raise `max_inflight` if the
  gateway has headroom to serve more.
- **`gateway_degraded`** — SDK retries or `GatewayQueueTimeout`s **while slots are NOT
  saturated**. The gateway itself is slow or failing; raising `max_inflight` won't help
  and the lever is the gateway (or its per-deployment limits), not this knob.

Retries plus a full lane is expected back-pressure. Retries with slots to spare is the
gateway, not you.

## Roll it out — Vera first, then PM

Follow ADR 0115's order; don't flip the whole fleet at once:

1. **Ship with `max_inflight: 0`.** No behaviour change; the snapshot reports
   `enabled: false`.
2. **Enable on Vera** with a limit below the gateway's real parallel capacity — for
   example `6` with `inflight_interactive_reserve: 1`. Watch `llm_queue_wait_seconds` and
   the SDK retry counts for about a week: wait time should be bounded and the
   synchronized retry bursts should mostly stop.
3. **Enable on the PM instance**, sized so the per-process totals across Vera + PM stay
   within the gateway's capacity.
4. A non-zero default is left to a later ADR amendment, once the metrics support it.

## Related

- [ADR 0115 — the in-flight limiter design](../adr/0115-gateway-inflight-limiter.md)
- [LiteLLM gateway](../explanation/litellm-gateway.md) — why every call resolves to a lane, and where the backstop limits live
- [Wire Langfuse + Prometheus](./observability.md) — scraping `/metrics` and the telemetry surface
- [Configuration reference](../reference/configuration.md) — the `model.*` keys
- [Operator API reference](../reference/operator-api.md) — `GET /api/telemetry/llm-lanes`
