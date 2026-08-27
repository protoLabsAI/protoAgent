# 0107 — Context Architecture v2: lifecycle, projection, typed memory

Status: **Proposed** (umbrella tracker #3184; gate for all implementation work)

## Context and Problem Statement

The context architecture — how knowledge enters a model call, what the model
sees, what gets persisted, and how different runtimes receive it — spans eight
ADRs (0021, 0033, 0041, 0060, 0069, 0079, 0101, 0102) but no single record
describes the whole lifecycle from storage through delivery to observation.
Five structural problems have accumulated:

1. **Derived context is persisted as history.** `KnowledgeMiddleware` composes
   recalled memory, the skills index, and working state per turn and delivers
   them as a `HumanMessage` frame (ADR 0101 D2, `context_frame_message`). That
   frame is checkpointed like any other message. Over a long session the
   checkpoint accumulates stale snapshots linearly — each one a full copy of
   the injected context at the turn it was composed — inflating the history
   with content that was never operator speech and is out of date by
   construction.

2. **Memory axes are conflated in one field.** The `domain` column on
   `Chunk` serves simultaneously as topic bucket, memory kind, trust signal,
   and scope marker. A row with `domain="hot"` is always-on operator fact; one
   with `domain="session-summary"` is an episode digest; one with
   `domain="claude-import"` is untrusted inherited reference. Nothing in the
   schema distinguishes these roles — the middleware applies bespoke filtering
   per domain string, and a new memory kind requires editing both the store and
   every consumer.

3. **Runtime surfaces receive materially different context.** The native lead
   agent, a `task()` subagent, an ACP executor, a goal continuation, and a
   scheduler/watch background invocation each compose their context through
   different code paths with no documented contract for what each receives.
   Whether a runtime gets prior-session digest, hot memory, working state, or
   the skills index is an implementation detail, not a decision.

4. **Prompt observability captures only the system prompt.** Prompt snapshots
   (`observability/prompt_snapshots.py`) record the stable prefix per call but
   never the message list, the injected context frame, or the bound tools. ADR
   0102's trajectory log will record message references, but "what did the
   model actually see?" still requires assembling data from three stores — and
   some of those bytes may have been destroyed by pruning or compaction before
   anyone asks.

5. **The stable prompt names capabilities unconditionally.** The system prompt
   describes tool capabilities and behavioral contracts regardless of which
   tools are actually bound to the current graph build. An unbound capability
   (e.g. a plugin tool not loaded) is described as available, and the model
   sometimes attempts to use it — a form of prompt/toolset incoherence.

## Decision Drivers

- **Cache stability.** Anthropic's prefix-based prompt caching (the single
  largest cost lever — ADR 0101 audit measured 30.9% hit ratio vs ~99.9%
  achievable) requires byte-stable prefixes. Any change to the context
  architecture must preserve or improve cache discipline.
- **Context budget predictability.** The operator needs a reliable bound on
  how much of the context window is consumed by injected context vs.
  conversation history, and that bound must hold across runtimes.
- **Memory trust/authority separation.** Operator-authored facts, agent-derived
  findings, imported/ingested reference, and ephemeral session state have
  different trust levels and different lifecycles. The architecture must make
  these distinctions explicit rather than encoding them in domain strings.
- **Runtime surface parity.** Every runtime surface (lead, subagent, ACP, goal,
  scheduler, plugin API) should receive context governed by one contract. Parity
  means honest — each surface gets what it needs, documented — not identical.
- **Backward compatibility.** Existing checkpoints with tagged context frames
  must continue to load. The migration cannot require a coordinated cutover.
- **Observability.** "What did the model actually see on call N?" must become
  answerable from durable records without reconstructing it from scattered
  stores.

## Considered Options

### Option A: Status quo

Keep the current architecture — `KnowledgeMiddleware` composes per turn,
delivers as a checkpointed `HumanMessage` frame, `domain` stays overloaded,
each runtime wires its own middleware stack. Extend incrementally with more
domain strings and per-consumer filters.

**Rejected.** The conflation problems compound: every new memory kind requires
editing the store, the middleware, and every runtime that filters by domain.
The checkpoint inflation is linear in session length and drives compaction
frequency, which in turn destroys forensic data that ADR 0102 needs to
preserve.

### Option B: Full rewrite

Replace the knowledge store with a typed event-sourced system. Redesign the
checkpoint format to exclude derived context. Migrate all runtimes to a single
composition pipeline in one release.

**Rejected.** The blast radius is too wide for the payoff. The physical store
(one SQLite file, FTS5 search) is sound — the problem is the schema on top of
it and the delivery pipeline, not the storage engine. A full rewrite would
also require a coordinated checkpoint migration that blocks every other
initiative.

### Option C: Incremental invariant-based migration (chosen)

Establish four architectural invariants (D1-D4 below) and migrate toward them
in independently-revertible phases. Each phase has a rollback seam. The
physical store stays; the schema gains typed axes. The delivery pipeline is
refactored to project at request time rather than persist. Runtimes converge
on one contract with explicit adapters.

## Decision Outcome

**Option C — incremental invariant-based migration**, governed by four
invariants:

### D1: Durable state and model-visible context are separate

The knowledge store and the checkpoint hold **durable state** — facts, episode
summaries, operator notes, working-state snapshots. The model-visible context
is a **projection** derived at request time by a projector that reads durable
state and composes what the model needs for this call.

Today `KnowledgeMiddleware.compose_context` already performs this projection,
but its output is checkpointed as a `HumanMessage` (via `context_frame_message`).
Under D1 the projector's output is consumed at the `wrap_model_call` seam and
never enters the checkpoint. The tagged frame becomes a transient request
artifact, not a persisted message.

**Compatibility.** Old checkpoints contain tagged frames
(`CONTEXT_FRAME_KWARG = "protoagent_injected_context"`). The projection
pipeline strips these on read — the same `is_context_frame` predicate already
exists — and re-composes from current durable state. A session started on the
old code and resumed on the new one sees fresh context, not stale snapshots.

### D2: One physical backend with typed memory axes

The knowledge store remains one SQLite file with one `chunks` table. The
`domain` column stays as a **topic bucket** (its original purpose: "memory",
"session-summary", "hot", "claude-import", etc.). Four new columns provide
orthogonal classification:

| Column         | Type    | Purpose                                              |
|----------------|---------|------------------------------------------------------|
| `memory_kind`  | TEXT    | What kind of memory this is (see Typed Memory Model) |
| `subject`      | TEXT    | The entity this row describes (nullable)             |
| `review_state` | TEXT    | Lifecycle: candidate / confirmed / rejected          |
| `expires_at`   | TEXT    | Optional expiration (ISO 8601 datetime, nullable)    |

These are **axes**, not replacements for domain. A hot operator fact has
`domain="hot"`, `memory_kind="standing"`, `review_state="confirmed"`. A
session summary has `domain="session-summary"`, `memory_kind="episode"`,
`review_state="confirmed"`. The axes are queryable independently and
composable in delivery policy.

### D3: Four context layers with stable ordering

Every model call sees up to four layers, always in this order:

1. **Stable prefix** — identity (`SOUL.md`), behavioral invariants, capability
   doctrine. Byte-stable within a graph build. Carries Anthropic's
   `cache_control` breakpoint. This is the system message.

2. **Session-frozen snapshot** — prior-session digest, operator profile facts,
   standing instructions. Composed once at session start (or on first turn) and
   frozen for the session. Stable within a session, so it caches after the
   first call.

3. **Current-turn delta** — hot memory, RAG retrieval results, working state,
   skill index. Composed per turn (not per call within a tool loop). This is
   the part that churns, but it churns only at turn boundaries, not mid-tool-loop.

4. **On-demand retrieval** — tool-invoked knowledge lookups, loaded skill
   procedures. These enter the message stream as tool results and are not
   injected by the middleware.

Layers 1-3 are ordered so that each is a prefix of the next: layer 2 extends
layer 1, layer 3 extends layer 2. This maximizes Anthropic's prefix cache hit
rate because each successive call shares the longest possible prefix with the
previous one.

### D4: One semantic runtime context contract

All runtime surfaces — native lead, subagent, ACP, goal continuation,
scheduler/watch, plugin/API, incognito — receive context through **one
contract** with **explicit delivery adapters** per surface.

The contract specifies:

- Which memory axes are injected (by `memory_kind`, `review_state`, trust tier)
- Which prompt planes are included (identity, behavioral, capability, runtime)
- Whether working state is visible
- Whether writes back to the knowledge store are permitted
- The trust boundary (what the surface may treat as instructions vs. data)

Each runtime surface has a named adapter that implements the contract for its
specific constraints (e.g., a subagent adapter strips prior-session digest to
conserve context budget; an incognito adapter strips all memory injection).

## Typed Memory Model

The `memory_kind` axis classifies every knowledge-store row by what kind of
memory it represents. The values and their semantics:

| `memory_kind` | Description | Typical source | Lifecycle |
|---|---|---|---|
| `profile` | Stable attributes of an entity (name, role, preferences) | Operator input, extraction | Long-lived, updated on change |
| `standing` | Always-on instructions or facts (today's "hot" memory) | Operator `memory_ingest`, config | Persistent until revoked |
| `fact` | A specific learned fact or finding | Conversation extraction, harvest | Persistent, may be superseded |
| `decision` | A recorded decision and its rationale | Extraction, ADR reference | Persistent |
| `note` | A free-form operator note | Operator input | Persistent |
| `episode` | A session summary or event digest | `SessionSummaryMiddleware` | TTL-bounded or capped |
| `reference` | Imported/ingested external content | `claude-import`, RSS, docs | Trust-tagged, may expire |
| `legacy` | Pre-typed rows that have not been migrated | Migration backfill | Transitional |

**Why `domain` stays as a topic bucket.** `domain` predates this architecture
and is used pervasively — in the store's FTS queries, in the middleware's
injection filtering, in the injection log, in the API's search/list endpoints,
and in operator-facing UI. Redefining it as `memory_kind` would require a
coordinated migration of all consumers and break the API contract. Instead,
`domain` keeps its role as a free-form topic label (an operator can create
domain "project-alpha" or "competitor-analysis"), and `memory_kind` provides
the orthogonal structural classification the delivery pipeline needs.

**`subject`** names the entity a row describes — a person, project, tool, or
concept. It enables queries like "everything known about project X" across
memory kinds without relying on FTS relevance scoring.

**`review_state`** governs the confidence lifecycle:
- `candidate` — agent-derived, not yet validated (e.g., a freshly extracted
  fact). May be injected with lower trust framing.
- `confirmed` — operator-approved or high-confidence. Injected at full trust.
- `rejected` — explicitly marked as wrong. Retained for audit (ADR 0069 D9
  invalidation) but never injected.

**`expires_at`** is an optional lifecycle boundary. Rows past their expiration
are excluded from injection but retained in the store for forensics. Episode
summaries, for example, might expire after 30 days to keep the prior-session
digest bounded.

## Request-Only Projection

### How `KnowledgeMiddleware` changes

Today `compose_context` returns `{"context": str, "context_sections": list}`,
which `before_model` wraps in a `context_frame_message` and returns as a state
update. The frame is checkpointed.

Under D1, the projection pipeline changes:

1. `compose_context` continues to compose the same content (layers 2-3 above),
   but returns a `ContextProjection` dataclass instead of a raw dict.
2. The projection is consumed at the `wrap_model_call` seam — the same
   position `PromptCacheMiddleware` and `PromptCaptureMiddleware` already
   occupy. The projector inserts the composed content into the request's
   message list as a transient frame that the model sees but the checkpoint
   does not.
3. `before_model` clears the `context` and `context_sections` state channels
   (as it already does when composition is empty) rather than populating them
   with the frame.
4. `PromptCaptureMiddleware` records the projection in its snapshot (addressing
   observability gap #4).

### Compatibility with old checkpoints

When the projection pipeline encounters a message with
`CONTEXT_FRAME_KWARG = True` in checkpoint history, it strips it from the
message list before the model call and re-composes from current durable state.
The `is_context_frame` predicate already exists for this purpose. Sessions
started on the old code resume seamlessly — the first new-code call strips
stale frames and projects fresh context.

## Prompt Contracts

The stable prefix (layer 1) is decomposed into four **prompt planes** with
explicit ownership:

| Plane | Source | Content | Stability |
|---|---|---|---|
| **Identity** | `SOUL.md` + `graph/prompts.py` | Agent name, persona, voice, core mission | Stable across builds (operator-edited) |
| **Behavioral invariants** | `graph/prompts.py` | Response format, safety rails, structured-output protocol, trust policy | Stable within a release |
| **Capability doctrine** | Generated from bound tools + loaded plugins | What the agent can do — tool descriptions, plugin capabilities, MCP server summaries | Stable within a graph build; regenerated on hot reload |
| **Runtime adapter** | Per-surface adapter (D4) | Surface-specific instructions (e.g., subagent delegation protocol, ACP executor constraints) | Stable per surface type |

**Capability doctrine is generated from bindings, not declared.** Today the
system prompt describes capabilities by name regardless of whether they are
bound. Under this contract, the capability plane is generated from the actual
tool bindings and plugin registrations of the current graph build. An unbound
capability produces no doctrine text — the model is not told about tools it
cannot call.

## Runtime Surface Matrix

The following table documents what each runtime surface receives under the D4
contract. "Yes" means the surface receives that component; "No" means it is
excluded; qualifications are noted.

| Surface | Memory injection | Working state | Prompt planes | Persistence | Write-back | Trust boundary |
|---|---|---|---|---|---|---|
| **Native lead** | Full (digest + hot + RAG) | Yes | All four | Checkpoint + trajectory | Yes (harvest, hot) | Operator authority |
| **Subagent (`task()`)** | Hot only (no digest, no RAG) | Parent snapshot (read-only) | Identity + behavioral + capability (own tools) | Parent checkpoint (final message only) | No (parent consolidates) | Delegated, tool-fenced |
| **ACP executor** | None | None | Behavioral + runtime adapter only | Own checkpoint | No | Sandboxed |
| **Goal continuation** | Hot only (no digest) | Yes (own plan/tasks) | All four | Checkpoint + trajectory | Yes (plan updates) | Operator authority, no digest bias |
| **Scheduler/watch/background** | Hot only | Relevant watches/schedules | Identity + behavioral + capability | Checkpoint | Yes (findings, alerts) | Operator authority, bounded context |
| **Plugin/API** | Configurable per plugin | Configurable | Identity + behavioral + plugin adapter | Plugin-scoped | Plugin-scoped | Plugin trust tier |
| **Incognito** | None (ADR 0069 D3b) | Yes (operational, not memory) | All four | Checkpoint (no session memory) | No memory writes | Operator authority, memory-isolated |

## Prompt Observability

### The gap

Today's prompt snapshots (`observability/prompt_snapshots.py`) capture the
system prompt text per model call. They do not capture:

- The injected context frame (which is a HumanMessage, not part of the system prompt)
- The message history sent to the model
- The bound tool schemas
- The model parameters (temperature, thinking config, etc.)

### The fix

At the `wrap_model_call` seam — after `PromptCacheMiddleware` has placed
breakpoints and the projector has inserted the transient context frame —
`PromptCaptureMiddleware` records a **complete model-visible request
descriptor**:

- Stable-prefix hash (already computed)
- Context projection summary (layer, char counts, memory-kind breakdown)
- Message count and cumulative chars (per role)
- Bound tools hash + count
- Model identifier and parameters

This descriptor is both persisted to the prompt snapshot store (extending the
existing capture) and emitted as the `request` event in the ADR 0102
trajectory log. The two consumers share one writer — the capture middleware
does the work once.

The goal is not to duplicate every byte (that is the trajectory's optional
`full_text` mode), but to make the *shape* of every model call inspectable
without reconstruction from multiple stores.

## Migration Order

The migration proceeds in four phases with explicit dependencies. Each phase
has a rollback seam — the changes can be reverted independently without
breaking the phases that precede it.

### Phase 0: Foundation (this ADR)

- This ADR (#3192) — the governing decision record
- Characterization fixtures — tests that document today's context shape so
  regressions are caught immediately

### Phase 1: Projection + observability + prompt contracts (parallel)

- **#3188 — Request-only projection.** Refactor `KnowledgeMiddleware` to
  project at `wrap_model_call` without checkpointing the frame. Compatibility
  shim strips old frames on read.
- **#3191 — Prompt observability.** Extend `PromptCaptureMiddleware` to record
  the full model-visible request descriptor. Wire into ADR 0102 trajectory.
- **#3190 — Prompt contracts.** Split the stable prefix into the four prompt
  planes. Generate capability doctrine from bound tools only.

These three are independent and can ship in any order.

### Phase 2: Typed memory schema (parallel with Phase 1)

- **#3072 — Typed memory axes.** Add `memory_kind`, `subject`,
  `review_state`, `expires_at` columns to the chunks table. Backfill existing
  rows. Migration is additive (new columns with defaults) — the old code
  ignores columns it does not query.

### Phase 3: Delivery + write lifecycle (depends on #3188 + #3072)

- **#3187 — Delivery policy.** The projection pipeline uses typed axes (not
  domain strings) to decide what to inject per runtime surface.
- **#3185 — Write lifecycle.** `review_state` transitions (candidate ->
  confirmed, expiration sweeps) are implemented as store operations, not
  middleware side effects.

### Phase 4: Runtime contracts + eval (depends on #3188)

- **#3189 — Runtime context contracts.** Formalize the D4 contract as adapter
  interfaces. Each runtime surface gets a named adapter.
- **#3186 — Prior-session eval.** Evaluation harness for memory injection
  quality — does the projected context actually help the model? Requires the
  projection pipeline (#3188) to be in place so the eval measures the new
  path.

### Rollback seams

- Phase 1 changes are behind the existing `KnowledgeMiddleware` toggle
  (`middleware.knowledge: true/false`). The old `before_model` path is
  retained as a fallback for one release.
- Phase 2 is additive schema — rollback is "ignore the new columns."
- Phase 3 is a policy change atop Phase 2 — rollback is "use the old
  domain-based filtering."
- Phase 4 is new code with no removal of old paths — rollback is "don't
  register the adapter."

## Non-Goals

- **Physical store partitioning.** The knowledge store stays as one SQLite
  file. Splitting into per-kind databases adds operational complexity (backup,
  migration, cross-kind queries) without proportional benefit at current scale.

- **Real-time vector reranking.** `HybridKnowledgeStore` already supports
  RRF fusion of FTS5 + vector rankings. A learned reranker (cross-encoder,
  ColBERT) is deferred — the typed axes and delivery policy provide the
  structural filtering that reranking cannot (trust, lifecycle, scope), and
  FTS5 + RRF is adequate for the current knowledge-store scale.

- **Cross-instance memory federation.** Memory stays instance-scoped (ADR
  0004, ADR 0041). Fleet members share context through the delegation
  protocol (A2A, `task()`), not through a shared knowledge store. Federation
  is a separate initiative if it ever becomes necessary.

## References

- **#3184** — Context Architecture v2 umbrella tracker
- **#3072** — Typed memory schema
- **#2806** — Trajectory log umbrella (ADR 0102)
- **#3073** — Memory lifecycle hooks
- **#3185** — Write lifecycle
- **#3186** — Prior-session eval
- **#3187** — Delivery policy
- **#3188** — Request-only projection
- **#3189** — Runtime context contracts
- **#3190** — Prompt contracts
- **#3191** — Prompt observability
- **ADR 0021** — Agent memory: extract, don't dump
- **ADR 0033** — Pluggable agent runtime (ACP executor)
- **ADR 0041** — Workspaces & tiered stores
- **ADR 0060** — Skills: progressive disclosure
- **ADR 0069** — Memory delivery layer
- **ADR 0079** — Autonomous operating model
- **ADR 0101** — Context lifecycle: log, surface, pressure
- **ADR 0102** — The trajectory: session log + derived surface
