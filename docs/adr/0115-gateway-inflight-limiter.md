# 0115 — Model calls share a bounded, priority-ordered in-flight lane per endpoint and model (off by default)

- Status: Proposed
- Date: 2026-09-28
- Builds on: [ADR 0006](./0006-observability-and-the-self-improving-flywheel.md). The queue-depth and wait-time signals extend its Prometheus and telemetry surfaces. [ADR 0106](./0106-provider-registry.md) is why a lane is keyed by the connection's resolved endpoint and model, not by a single legacy `model.api_base`. [ADR 0025](./0025-unified-delegate-registry-and-panel.md) covers ACP delegates, which are separate processes that this limiter cannot see (D7).
- Refs: protoLabsAI/pr-reviewer-plugin#209 (throughput comment); board analysis bd-mpak §1–2, §4 card 5.

## Context

**Nothing in protoAgent limits how many model calls are in flight at once.** `graph/` contains no `Semaphore` or `BoundedSemaphore` (searched at write time). The only concurrency caps are local to one fan-out:
- `subagent_max_concurrency: int = 4` (`graph/config.py:1228`). It becomes a fresh `asyncio.Semaphore` for each `task_batch` call (`graph/agent.py:1161–1163`), and the config comment itself says a single `task` is unbounded (`graph/config.py:1223–1227`).
- A workflow recipe's own `max_concurrency` overrides the caller's value (`plugins/workflows/engine.py:253`), with a ceiling of `MAX_FANOUT = 16` (`plugins/workflows/engine.py:44–46`). The ceiling guards against a typo; it does not limit the gateway.

None of these caps knows about the others, so the process-wide total is the sum of every fan-out running at that moment.

**The #209 incident.** Two boards were auto-opening PRs into one review gate (Vera), and no review finished for 26 minutes (09:00→09:26Z, 2026-09-27). Vera's log was almost entirely `openai._base_client Retrying request … (retry 1 of 2 / 2 of 2)` bursts about every 2 minutes. Per bd-mpak §2, the gateway load was:
- 3 panels (`max_concurrent_panels` default 3; pr-reviewer `webhook.py:99–105`), each running `workflows/code-review-structural.yaml:31` `max_concurrency: 5`. That is up to 15 finder calls at once.
- protoPatch clawpatch calls on top of that.
- Coders from two boards, all on the same gateway lane.

The gateway was accepting connections but not answering in time, so the fleet did not queue. Every caller timed out and retried together.

**The timeout and retry stack.** Each layer multiplies the one before it:

| Layer | Value | Where |
|---|---|---|
| HTTP request timeout | `request_timeout: float = 120.0` | `graph/config.py:1183`; YAML `model.request_timeout` (`config.py:2404`); passed as `"timeout"` at `graph/llm.py:770` |
| SDK retries of the request start | `llm_max_retries: int = 2` (3 attempts) | `graph/config.py:1184`; YAML `model.max_retries` (`config.py:2405`); `"max_retries"` at `graph/llm.py:771` |
| Stream reconnect before any content | same `max_retries` (3 attempts), backoff 0.5 s doubling | `_stream_with_reconnect`, `graph/llm.py:132–189` (`attempts` at 156; sleep/double at 187–189) |
| Per-chunk stall guard | `request_timeout` for first token and between chunks | `_guard_stream_timeout`, `graph/llm.py:201–241`; composed by `_guarded_reconnecting_stream`, `graph/llm.py:244–264` |
| Where the OpenAI-compatible client composes the layers | — | `_ReasoningChatOpenAI._astream` / `_stream_measured`, `graph/llm.py:556–605` (call at 589–594) |
| Same composition, anthropic-oauth client | — | `graph/providers/anthropic_oauth.py:173–192`; kwargs at 270–271 |
| Same composition, openai-codex client | `CodexChatOpenAI(_ReasoningChatOpenAI)` inherits `_astream` | `graph/providers/codex_client.py:210`; kwargs at `graph/providers/openai_codex.py:102–103` |

**Worst case for one logical call.** The SDK allows 3 × 120 s ≈ 6 minutes. A stall or drop before any content repeats that up to 3 times, so about 18 minutes. The failure also feeds itself: each timed-out request is re-sent while the gateway may still be working on the abandoned one (unless it propagates the disconnect). More load produces more timeouts, which produce more retries. The repo already reached the same conclusion for embeddings: *"No client retries: the gateway owns retries/fallbacks; app-side retries only multiply the hang"* (`graph/llm.py:988–993`, `max_retries=0` at 1008). Chat calls have no equivalent protection.

**What we want instead:** when the lane is saturated, extra callers wait in a bounded local queue that the rest of the system can see. They should not all send requests that are bound to time out.

## Options considered

**(a) An in-process asyncio limiter around model invoke/stream in `graph/llm.py`, keyed per endpoint and model lane.** This is the one place every in-process chat-model call already passes through. Both stream clients and codex go through `_guarded_reconnecting_stream`, so one seam covers all three. The limiter is cheap and host-testable, and it can separate queue wait from HTTP time exactly. Its limit is that it only sees the process it runs in. **Adopted (D1–D5).**

**(b) Per-caller budgets: fixed slot quotas per class (interactive / review panel / board coder).** Hard partitions strand capacity: an idle interactive quota cannot serve a panel burst. The classes also do not line up with processes. Board coders are ACP subprocesses (D7), and panels run in Vera's process. **Partially adopted.** We keep the useful part: a priority order on the wait queue plus a small reserve for interactive callers (D6). We reject hard per-class quotas.

**(c) Rely on gateway-side limiting only (LiteLLM per-key or per-deployment parallel-request / rpm / tpm limits).** This is the only layer that sees every process and every ACP coder. But it answers overload with an immediate rejection (429), not a priority queue. A 429 goes straight back into the same SDK retry loop. The gateway cannot tell an operator's chat turn from the eleventh finder in a panel, and its queue is invisible to our boards. **Rejected as the primary mechanism, recommended as the cross-process backstop** (Consequences). The two layers work together: a local limiter sized below the gateway limit keeps 429s rare.

**(d) A cross-process limit per box** (for example an flock/SQLite token table under the box root, shared by Vera, PM, and the other instances). This would handle the "Vera and PM in separate processes" case. The costs:
- lease expiry when a process dies
- clock and fairness across processes
- a new shared-state file on the hot path of every model call

It still would not cover ACP coders or clawpatch, which are not protoAgent processes. **Out of scope for this ADR.** Each process gets its own budget (D1), and the operator sizes the budgets so they add up to the gateway's capacity. We revisit this when the metrics in D8 show several processes on one box saturating a lane together while each stays under its own limit.

## Decision

**D1 — Lanes and limit.** A lane is `(resolved base_url, model id)`, the same key shape as `_window_key` (`graph/llm.py:~608`). For anthropic-oauth the key is `anthropic-oauth|<model>`, and for openai-codex it is the codex endpoint and model. Each lane has one limiter per process, created lazily in a registry in the new module `graph/llm_limiter.py`. Every lane in a process gets the same `max_inflight`; per-lane overrides are future work. Callers that do **not** go through a lane:
- embeddings, which have their own 8 s timeout and 0 retries (`graph/llm.py:988–1008`)
- raw `gateway_client` / `gateway_sync_client` httpx calls (images, transcription)
- `acp:` aux models

**D2 — Config keys.** Each key gets a `graph/settings_schema.py` FIELDS entry in section "Model & runtime", `scope="agent"`, hot-reloadable, following the `model.turn_stall_timeout_seconds` entry at `settings_schema.py:205–216`:

| YAML key | Attr | Default | Meaning |
|---|---|---|---|
| `model.max_inflight` | `llm_max_inflight` | **0 (off)** | Maximum concurrent model calls per lane in this process. 0 disables the limiter entirely (today's behaviour, and zero overhead beyond one branch). |
| `model.inflight_queue_timeout` | `llm_inflight_queue_timeout` | **300** (s) | Longest one acquisition may wait for a slot. Separate from `request_timeout`; kept below `turn_stall_timeout_seconds` (900, `config.py:1150`) so a queued turn fails with a queue error, not a stall. |
| `model.inflight_interactive_reserve` | `llm_inflight_interactive_reserve` | **1** | Slots that only `interactive` callers may take (D6). Clamped to `max_inflight − 1`, so non-interactive work always has at least one slot. |

The scope is `agent`, not `host`, deliberately. A box-wide default would silently give each process N slots. Per-process sizing is the explicit contract (Options d). A config change applies to acquisitions made after the reload; slots already held are not revoked.

**D3 — What counts as in flight.**
- **Streaming (the normal path, `streaming=True` at `llm.py:~780`):** the call holds a slot from just before it issues the HTTP request until the stream ends, which is one of: exhausted (`StopAsyncIteration`), raised, or closed or cancelled by the consumer (the `aclose` in `_guard_stream_timeout`'s `finally`). The slot covers token generation because the backend is busy for that whole time, not just until the first byte.
- **Non-streaming `_agenerate`:** the slot is held around the call.
- **Between model rounds of a turn:** no slot is held, because each model round is a separate call. Tool execution never holds a slot.
- **Waiting and cancellation:** a waiter that is cancelled or times out is removed from the queue and leaks nothing.

**D4 — Queue wait vs `request_timeout`.**
- The slot is acquired **outside** `_guard_stream_timeout`, inside the per-attempt factory in `_guarded_reconnecting_stream` (`llm.py:259–264`), as the outermost wrapper. So neither httpx's timeout nor the per-chunk guard starts until the slot is held: wait time never counts toward `request_timeout`.
- Wait time has its own bound, `model.inflight_queue_timeout`. When it expires the caller gets `GatewayQueueTimeout(TimeoutError)`. Its message names the lane, the wait time, and the queue position.
- `GatewayQueueTimeout` is **not** in `RETRYABLE_STREAM_ERRORS`. Retrying a queue timeout just rejoins the queue, which would reproduce the #209 amplification locally.
- Because acquisition sits outside the guard, the guard's `except TimeoutError → StreamStallTimeout` translation (`llm.py:227–232`) can never mislabel a queue timeout.

**D5 — Retries and the slot.**
- **SDK retries stay inside the slot.** `openai`'s retry of the request start happens inside a single `super()._astream` attempt, and we cannot release the slot between its attempts without patching the SDK. One attempt is one unit of backend work; its SDK retries are part of it.
- **Stream reconnects re-acquire.** Each `_stream_with_reconnect` attempt calls `make_stream()` again (`llm.py:156–160`), so the slot is released before the backoff sleep and requested again for the next attempt. The waiter keeps its **original arrival time**, so aging (D6) counts from the start of the logical call.
- **Budget, not re-tuning.** A reconnect never waits longer than `inflight_queue_timeout` per acquisition. We do not lower `llm_max_retries` when the limiter is on. With the limiter sized correctly, timeouts, and therefore retries, should mostly stop, and the D8 metrics will show whether that is true. Revisiting SDK retries in favour of gateway-owned retries (the embeddings precedent) is a separate decision.

**D6 — Fairness and priority.** Waiters are served strictly by class, then first-come-first-served within a class:
- `interactive`: chat or console turns an operator is watching
- `default`: A2A, background and scheduled turns, subagents
- `bulk`: workflow fan-outs, sweeps, review-panel finders

Two rules prevent starvation or lock-out:
- **Aging.** A waiter's effective class rises one step for every 60 s it has waited (an internal constant, not a config key), so bulk work cannot starve.
- **Reserve.** `inflight_interactive_reserve` slots are granted only to `interactive` waiters. A panel burst cannot take the last slot from the operator's chat.

The class is carried in a `contextvars.ContextVar` (the same pattern as `_REQUEST_MEASURE`, `llm.py:325`), so subagent tasks inherit it:
- `server/chat.py` sets `interactive` for chat and console turns.
- `plugins/workflows/engine.py` sets `bulk` for recipe fan-outs.
- Anything unset is `default`.
- Plugins get `sdk.llm_priority("bulk")`, a context manager in `graph/sdk.py`, so pr-reviewer can mark its panels explicitly.

We do not implement weighted fair queuing between classes; strict order plus aging is enough at these queue depths, and simpler to reason about.

**D7 — Coverage boundary.** The limiter covers every chat-model call made **in this process** through `create_llm`: the gateway (`_ReasoningChatOpenAI`), codex (its subclass), and anthropic-oauth. It does **not** cover:
- **ACP coder delegates** (claude-code, proto/protoCLI, opus/sonnet): separate processes that call their endpoints with their own clients.
- **clawpatch:** a pr-reviewer subprocess.
- **Other protoAgent instances on the box.**

Those callers are bounded by their own concurrency settings (the board's `max_concurrent`, pr-reviewer's `max_concurrent_panels`, protoPatch's time budget) and by gateway-side limits (Options c). The limiter makes no claim to protect a lane from them. The D8 metrics show local saturation separately from gateway slowness, and that is what lets an operator tell which case they are in.

## Observability

**D8 — Signals.**

**Prometheus**, via `observability/metrics.py`. `Gauge` is already imported there, next to `record_llm_call` at `metrics.py:200`. Names follow its `{prefix}_` convention:
- `{p}_llm_inflight{lane}` (gauge)
- `{p}_llm_queue_depth{lane,priority}` (gauge)
- `{p}_llm_queue_wait_seconds{lane,priority}` (histogram; buckets 0.1, 0.5, 1, 5, 15, 30, 60, 120, 300)
- `{p}_llm_queue_timeouts_total{lane}` (counter)

**In-process snapshot.** `graph.llm_limiter.snapshot()` returns this, and plugins read it through `sdk.llm_lanes()` in `graph/sdk.py`:

```json
{"enabled": true, "generated_at": "…",
 "lanes": [{"lane": "https://gw/v1|protolabs/smart", "limit": 6, "reserve": 1,
            "inflight": 6, "queued": 9, "queued_by_priority": {"interactive": 0, "default": 2, "bulk": 7},
            "oldest_wait_s": 212.4, "wait_p50_s_5m": 38.0, "wait_p90_s_5m": 171.0,
            "queue_timeouts_5m": 2, "saturated": true}]}
```

`saturated` is true when `queued > 0` has held continuously for 60 s or more.

**HTTP.** `GET /api/telemetry/llm-lanes` in `operator_api/telemetry_routes.py`, next to `/api/telemetry/summary` (`telemetry_routes.py:344`). It returns the same payload, reads memory only, and is cheap enough to poll every tick. When the limiter is off it returns `{"enabled": false}`.

**How pr-reviewer uses it (card bd-8whx and its `/queue` endpoint).** pr-reviewer runs inside Vera's process, so it reads `sdk.llm_lanes()` directly, with no HTTP call. bd-8whx currently infers `gateway_degraded` from SDK retry log lines. With lanes available it can tell two states apart:
- `local_queue`: `saturated`, or `wait_p90_s_5m` above a threshold. The gate is slow because this process is queuing on purpose, and ETAs should use queue depth.
- `gateway_degraded`: retries or timeouts while slots are **not** saturated. The gateway itself is slow or failing.

`/queue` adds `lane_queue_depth`, `lane_wait_p90_s`, and `lane_saturated` for the lane its panels use. This is a follow-up in pr-reviewer-plugin, not in this split.

**How board holds use it.** projectBoard already reads pr-reviewer's `/queue` for in-review cards (bd-mpak card 6). With the lane fields there, a `review-wip-limit` or `review queued` hold can say "gate queue: N calls waiting, p90 wait M s", which separates "the gate is throttling" from "the gate is dead." A board can also poll its own process's `/api/telemetry/llm-lanes` before dispatching in-process work. ACP coders do not appear there (D7). These projectBoard changes are follow-ups outside this split.

## Consequences

**Benefits.** Saturation turns into queuing that the rest of the system can see, not a synchronised wave of timeouts and retries. The operator's chat keeps a slot during a panel burst. `request_timeout` goes back to meaning backend latency, not time spent in the queue.

**Costs and risks:**
- **Per-process only.** Two instances with `max_inflight: 6` each can still put 12 calls on a gateway sized for 8. Operators must size the budgets together, and gateway-side limits are the backstop.
- **Throughput risk if set too low.** A limit below what the gateway can actually serve reduces throughput. That is why the default is off.
- **New failure mode.** `GatewayQueueTimeout` is a new error class for callers. Chat already surfaces errors with the provider/model name; its message has to name the lane and the setting to change.
- **Retries still hold the slot.** SDK retries inside a slot still occupy it (D5). Removing them is a separate decision.
- **Priority tagging is best effort.** Untagged work is `default`.

**Rollout:**
1. Ship with `max_inflight: 0`. There is no behaviour change and the snapshot reports `enabled: false`.
2. Enable on Vera with a limit below the gateway's real parallel capacity, for example 6 with reserve 1. Watch `llm_queue_wait_seconds` and SDK retry counts for a week.
3. Enable on the PM instance, sized so the per-process totals stay within the gateway's capacity.
4. Consider a non-zero default in a later ADR amendment once the metrics support it.

**Revisit when** metrics show several processes on one box saturating a lane together while each stays under its limit (then do Options d), or when timeouts continue with slots unsaturated (then the problem is the gateway, and Options c or re-tuning SDK retries is the lever).

**Host-side testing.** Everything is testable with fakes at the model boundary, with no gateway involved:
- The pure limiter module takes an injected clock, so aging, reserve, queue timeout, and cancellation are deterministic.
- A fake `_astream` that blocks on an `asyncio.Event` proves the in-flight definition (slot held until the stream closes or is cancelled).
- A fake stream that raises `httpx.ReadError` before any content proves that a reconnect releases and re-acquires the slot and keeps its arrival time.
- A fake slow first token proves that `request_timeout` starts after acquisition and that a queue timeout raises `GatewayQueueTimeout`, not `StreamStallTimeout`.
- `max_inflight: 0` is proven to be a pass-through by the existing `tests/test_stream_timeout_3699.py` and `tests/test_llm.py` staying green unchanged.

## Implementation split (coding cards; board only after verification)

Board on protoAgent. `changelog.d/**` fragments are breadth-excluded but listed. Each fragment's bold lead carries the tracking-issue `(#3760)` per #3291. All paths were checked at write time; `(new)` marks files the card creates.

**C1 — docs: ADR 0115 gateway in-flight limiter (docs-only)**
- files_to_modify: `docs/adr/0115-gateway-inflight-limiter.md (new)`, `docs/adr/index.md`, `plugins/docs/nav.json`, `changelog.d/inflight-limiter-adr.docs.md (new)`
- AC:
  1. The ADR text lands as verified, with Status: Proposed.
  2. There is one single-line index row after 0113.
  3. `python scripts/gen_docs_nav.py --check` passes.
  4. `npm run docs:build` has no dead links.
  5. `tests/test_docs_plugin.py` is green.
- depends_on: none

**C2 — graph/llm_limiter.py: per-lane priority limiter with queue timeout, reserve, aging, and snapshot**
- files_to_modify: `graph/llm_limiter.py (new)`, `tests/test_llm_limiter.py (new)`, `changelog.d/inflight-limiter-core.added.md (new)`
- AC:
  1. `acquire(lane, priority)` is an async context manager, and `limit=0` means no waiting.
  2. Waiters are served by class, then FIFO, with a one-step promotion per 60 s (injected clock).
  3. Reserved slots go only to `interactive` waiters, and the reserve is clamped to `limit−1`.
  4. Queue timeout raises `GatewayQueueTimeout(TimeoutError)` naming the lane, the wait, and the position.
  5. A cancelled or timed-out waiter leaves no residue.
  6. The priority ContextVar and `snapshot()` match the D8 shape, including the 5-minute p50/p90 and `saturated`.
  7. It has no imports from `graph.llm` or the config.
- depends_on: C1

**C3 — config: `model.max_inflight` / `inflight_queue_timeout` / `inflight_interactive_reserve` with settings FIELDS**
- files_to_modify: `graph/config.py`, `graph/settings_schema.py`, `tests/test_config_roundtrip.py`, `tests/test_settings_schema.py`, `changelog.d/inflight-limiter-config.added.md (new)`
- AC:
  1. The dataclass defaults are 0 / 300.0 / 1, parsed from the `model.*` keys next to `request_timeout` (`config.py:~2404`).
  2. FIELDS entries exist per D2 (section "Model & runtime", minimum=0, and descriptions stating per-process scope and that the queue bound is separate from `request_timeout`).
  3. The round-trip golden is updated and the schema tests are green.
- depends_on: C1

**C4 — graph/llm.py: acquire the lane slot per stream attempt (gateway, codex, anthropic-oauth)**
- files_to_modify: `graph/llm.py`, `graph/providers/anthropic_oauth.py`, `tests/test_llm_inflight.py (new)`, `changelog.d/inflight-limiter-wire.added.md (new)`
- AC:
  1. `_guarded_reconnecting_stream` takes an optional lane and acquires outside `_guard_stream_timeout` on each attempt, releasing in `finally`.
  2. The slot is held until the stream is exhausted, errors, or is cancelled.
  3. A reconnect releases before backoff and re-acquires, keeping its arrival time.
  4. Wait time does not start `request_timeout`.
  5. `GatewayQueueTimeout` is not retried and is not turned into `StreamStallTimeout`.
  6. `_agenerate` is also wrapped.
  7. Codex is covered through inheritance, with a test.
  8. `max_inflight: 0` passes through, and `tests/test_stream_timeout_3699.py` and `tests/test_llm.py` are unchanged and green.
- depends_on: C2, C3

**C5 — priority tagging: interactive chat turns, bulk workflow fan-outs, `sdk.llm_priority()`**
- files_to_modify: `server/chat.py`, `plugins/workflows/engine.py`, `graph/sdk.py`, `tests/test_llm_priority.py (new)`, `changelog.d/inflight-limiter-priority.added.md (new)`
- AC:
  1. Operator chat and console turns run under `interactive`, while A2A, background and scheduled turns stay `default`.
  2. Recipe fan-out steps run under `bulk`.
  3. `sdk.llm_priority(cls)` sets the class for its block, rejects unknown classes, and is inherited by subagent tasks.
  4. A test shows a queued `interactive` call overtaking queued `bulk` calls end to end.
- depends_on: C4

**C6 — observability: lane metrics, `GET /api/telemetry/llm-lanes`, `sdk.llm_lanes()`**
- files_to_modify: `observability/metrics.py`, `operator_api/telemetry_routes.py`, `graph/sdk.py`, `tests/test_llm_lanes_api.py (new)`, `changelog.d/inflight-limiter-observability.added.md (new)`
- AC:
  1. The four D8 metrics are registered and updated from the limiter, and are a no-op without `prometheus_client`.
  2. The route returns the D8 payload, or `{"enabled": false}` when off, and makes no network calls.
  3. `sdk.llm_lanes()` returns the same snapshot.
  4. It is tested with a fake saturated lane.
- depends_on: C5 (both edit `graph/sdk.py`; the edge keeps them in order)

**Order:** C1 → {C2, C3} → C4 → C5 → C6.

**Follow-ups outside this repo (not in this split):**
- pr-reviewer-plugin: split `local_queue` from `gateway_degraded` using `sdk.llm_lanes()`, and add the `lane_*` fields to `/queue`. Gate with `waits_for` on the protoAgent release that contains C6.
- projectBoard-plugin: quote the lane fields in the review hold's `next_action`.
