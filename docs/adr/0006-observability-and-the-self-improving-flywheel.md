# ADR 0006 — Observability & the Self-Improving Flywheel

- **Status:** Accepted (2026-06-01) — implementing the ranked plan (Slice 1 shipping alongside)
- **Date:** 2026-06-01
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** observability, cost, tracing, metrics, a2a, optimization, flywheel
- **Supersedes / Superseded by:** —

> Accepted. We want to *measure* what the agent actually costs and how long it
> takes — per LLM call, per tool, per turn — then turn that signal into a loop:
> **measure → analyze → optimize → measure**. The bones exist (Langfuse, a
> Prometheus module, an audit log, token capture, the outbound `cost-v1`
> DataPart), but the LLM half is half-wired (the `record_llm_call` metric is
> defined and never called), prompt-cache tokens and USD cost aren't captured,
> there's no historical store, and none of it surfaces in the console. This ADR
> records the current-state map, the flywheel target, the alignment with
> Workstacean's `cost-v1` A2A extension, and a sliced roadmap. **Slice 1
> (the measurement foundation) ships with this ADR.**

---

## 1. Context & Problem Statement

To refine cost and latency we first have to *see* them, reliably, at the right
granularity. An audit of what protoAgent captures today:

### What exists (good bones)

| Capability | Where | State |
|---|---|---|
| Langfuse tracing (session + tool spans, trace-id on audit lines) | `tracing.py` | Working; graceful no-op when unconfigured |
| Prometheus `/metrics` (per-fork namespaced) | `metrics.py`, `server.py` | Endpoint up; **LLM metrics defined but unused** |
| Per-tool latency + success → JSONL + Langfuse + Prometheus | `graph/middleware/audit.py` | Working |
| Per-LLM-call token capture (`on_chat_model_end`, `stream_usage=True`) | `server.py` `_run_turn_stream`, `graph/llm.py` | Working (input/output only) |
| Outbound `cost-v1` DataPart on the terminal A2A artifact | `a2a_handler.py` (`COST_MIME`, `_cost_payload`) | Working (usage + durationMs; `costUsd` omitted) |

### The gaps (what blocks the flywheel)

1. **`metrics.record_llm_call` is defined but never called** (`metrics.py`).
   The `*_llm_calls_total`, `*_llm_latency_seconds`, `*_llm_tokens_total` series
   are dead — the LLM half of any dashboard is empty. Only `record_tool_call`
   fires (from `AuditMiddleware`).
2. **No USD cost.** `costUsd` is deliberately omitted; there's no pricing table.
   Every consumer recomputes from tokens + its own rates.
3. **No prompt-cache tokens.** `usage_metadata.input_token_details` (cache_read
   / cache_creation) isn't read. Prompt caching is **on** — so we pay for the
   cache and can't see the savings or the hit ratio.
4. **No per-LLM-call latency.** Only whole-task wall-clock duration is computed
   (from `created_at`→`updated_at`).
5. **No historical store.** Prometheus is live-scrape-only (and missing the LLM
   data); `AuditMiddleware`'s session stats die on restart. Nothing inside
   protoAgent can answer "what was expensive/slow last week."
6. **The operator console shows none of it** — no tokens/cost/latency anywhere.
7. **No feedback loop.** Nothing turns telemetry into action.

## 2. The flywheel (target)

```
   ┌────────── measure ──────────┐
   │  per-call tokens (incl cache)│
   │  per-call + per-tool latency │
   │  USD cost, model, turn rollup│
   └──────────────┬───────────────┘
                  ▼
            persist + aggregate  (local store: per-turn / per-call rollups)
                  ▼
              surface  (operator console: $/turn, p50/p95, cache-hit %)
                  ▼
            optimize + feed back  (flag expensive/slow turns; prove the levers —
              prompt cache, tool deferral, compaction, model routing; feed
              signal into learned-skills / memory / operator recommendations)
                  ▲
                  └──────────── measure again ───────────┘
```

The levers already exist (prompt cache, `tools.deferred`, compaction,
routing/fallback). What's missing is the *measurement* that proves they work
and the *loop* that points them where they're needed.

## 3. Decision

Instrument at the seams the audit found; **don't** rebuild the backbone. Align
outbound telemetry with the fleet so the data is useful beyond protoAgent.

1. **Capture is per-LLM-call**, in `_run_turn_stream`'s `astream_events` loop —
   the only place with per-call visibility (a task may hit the model N times).
   Add cache tokens + per-call latency + the model name to what's already
   captured there.
2. **Cost is computed in-process** from a pricing table (`pricing.py`), cache-
   aware, and emitted as `costUsd` — making protoAgent the source of truth for
   its own cost rather than every consumer re-deriving it.
3. **Wire the dead metrics**: call `record_llm_call` per call; extend it with
   cache tokens + cost; add the missing series.
4. **Align with Workstacean's `cost-v1` A2A extension** (see §5) — emit the
   Anthropic-shaped cache fields it already expects, populate `costUsd`, and
   **declare the extension URI in the agent card** so the integration is
   explicit, not incidental.
5. **Persist + surface + close the loop** in later slices (§4).

## 4. Ranked Plan (slices)

1. ✅ **Measure (foundation) — ships with this ADR.** Capture cache tokens +
   per-call latency + model in `_run_turn_stream`; add `pricing.py` → `costUsd`
   (base input/output rates, fleet-consistent; cache-discounted cost deferred
   until gateway token semantics are validated); wire `record_llm_call`
   (extended with cache + cost series). Per-call **Langfuse** generation spans
   already come from the LiteLLM gateway callback — we deliberately *don't* add
   a manual shim that would bypass `trace_session`'s nesting (guarded by
   `test_no_legacy_shims_exist`). Emit cache fields + `costUsd` on `cost-v1` and
   declare the extension URI in the card. *(fixes 1–4)*
2. **Persist & aggregate.** A local telemetry SQLite (per-turn + per-call
   rollups: tokens, cache, cost, latency, model, tool counts) queryable inside
   protoAgent, scoped per instance like the other stores (ADR 0004). Survives
   restart; powers historical "what's expensive" analysis without external
   Prometheus/Langfuse. *(fixes 5)*
3. **Surface.** Operator-console telemetry: a per-turn footer (tokens · cost ·
   latency) and a session/▾history panel (cumulative cost, p50/p95 latency,
   cache-hit %, per-tool timings). Consumes the data already on the A2A stream.
   *(fixes 6)*
4. **Flywheel / feedback.** Flag turns whose cost/latency exceeds N× the rolling
   median; *prove the levers* (cache-hit %, deferral schema-token savings,
   routing/fallback rates, compaction triggers); feed signal back into the
   learned-skills / memory layer and operator recommendations. *(fixes 7)*

Priorities (per the kickoff): **cost visibility ($)** and **latency breakdown**
lead; Slice 1 makes both real. The console surface (Slice 3) follows so the
numbers are visible, then the loop (Slice 4).

## 5. Alignment: Workstacean's `cost-v1` A2A extension

protoAgent already emits a `cost-v1` DataPart; Workstacean already consumes one.
This ADR closes the remaining gaps so they meet the *same* contract.

- **Extension URI:** `https://proto-labs.ai/a2a/ext/cost-v1`
  (`protoWorkstacean/src/executor/extensions/cost.ts` `COST_URI`;
  `docs/extensions/cost-v1.md`). protoAgent will **declare it in the agent
  card's `capabilities.extensions`**, which is what gates Workstacean's cost
  interceptor (`ExtensionRegistry.interceptorsFor(card)`).
- **DataPart MIME:** `application/vnd.protolabs.cost-v1+json` (already matches).
- **`usage` shape** (Workstacean `lib/types/cost-v1.ts` `CostArtifactUsage`):
  `{ input_tokens, output_tokens, cache_creation_input_tokens?,
  cache_read_input_tokens? }` — Anthropic-shaped. **We don't emit the cache
  fields yet; Slice 1 adds them.**
- **`costUsd`** — Workstacean's interceptor uses our `costUsd` when present and
  only falls back to `tokens × MODEL_RATES` when it's absent. Emitting it makes
  us authoritative and sidesteps cache-discount mismatch (their `MODEL_RATES`
  in `lib/types/budget.ts` carries input/output only, no cache tiers).
- **Rates:** `pricing.py` mirrors the structure + overlapping values of
  Workstacean's `MODEL_RATES` and adds Anthropic cache multipliers
  (cache-read ≈ 0.1× input, cache-write ≈ 1.25× input). Both sides agree on the
  base rates; protoAgent's emitted `costUsd` is the cache-accurate number.
- **Consumer fields** (`docs/extensions/cost-v1.md`): the interceptor records a
  `CostSample` and publishes `autonomous.cost.{actor}.{skill}` for the planner's
  cost/confidence ranking and the fleet cost-per-outcome dashboard. So better
  protoAgent telemetry directly improves fleet-level planning — the flywheel
  isn't only local.

> Note: Workstacean's cost store is **observational, in-memory** (last 200
> samples/key), explicitly *not billing*. protoAgent's Slice 2 local store is
> the durable, queryable half on our side; the two are complementary.

## 6. Consequences

**Positive**
- Real per-call/per-turn cost + latency, with cache visibility — the data the
  flywheel runs on, and proof that the existing optimization levers work.
- Fleet alignment: one `cost-v1` contract; protoAgent's numbers feed
  Workstacean's planner + dashboards directly.
- Reuses the existing backbone (Langfuse/Prometheus/audit) — incremental, not a
  rewrite.

**Negative / costs**
- A pricing table is a maintenance surface — model rates drift; it must track
  the gateway (documented; falls back to a `default` rate, never crashes).
- Cache-token fidelity depends on the gateway surfacing
  `prompt_tokens_details` / `input_token_details` (OpenAI-compat exposes
  `cached_tokens`; Anthropic cache-creation may not round-trip through every
  gateway). Captured best-effort, defaulting to 0.
- Per-call instrumentation adds a little work to the hot streaming loop — kept
  cheap (counters + a contextvar), and all of it no-ops when
  Langfuse/Prometheus aren't configured.

## 7. Alternatives Considered

- **Lean entirely on Langfuse/Prometheus, no local store.** Good for live ops,
  but Langfuse is opt-in/external and Prometheus is scrape-windowed — neither
  gives protoAgent a queryable history for the flywheel. Local store (Slice 2)
  complements them, doesn't replace them.
- **Let consumers compute cost (status quo).** Keeps protoAgent rate-free but
  every consumer re-derives cost, no one sees cache savings, and the console
  can't show `$`. Rejected — emitting `costUsd` once, cache-aware, is the right
  source of truth.
- **A new bespoke telemetry DataPart.** Rejected — `cost-v1` already exists and
  is consumed fleet-wide; we conform to it rather than fork it.

## 8. Related

- [ADR 0004 — Multi-Instance Data Scoping](/adr/0004-multi-instance-data-scoping) — the Slice 2 store scopes per instance via the same helper.
- [ADR 0005 — Tool Pollution](/adr/0005-tool-pollution-and-progressive-disclosure) — `tools.deferred` is a lever Slice 4 will *prove* (schema-token savings).
- [Cost & trace propagation](/explanation/cost-and-trace), [Wire Langfuse + Prometheus](/guides/observability).
- Code: `server.py` (`_run_turn_stream`), `a2a_handler.py` (`cost-v1`),
  `graph/middleware/audit.py`, `tracing.py`, `metrics.py`; new `pricing.py`.
- Fleet: `protoWorkstacean/src/executor/extensions/cost.ts`,
  `protoWorkstacean/lib/types/cost-v1.ts`,
  `protoWorkstacean/docs/extensions/cost-v1.md`.
