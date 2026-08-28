# 0108 — Context Architecture v2: lifecycle, authority, and projection

Status: **Proposed** (umbrella #3184)

## Context

The context system has evolved through six ADRs (0021, 0033, 0041, 0060, 0069,
0101) and two major refactors (message-frame delivery #2776, rolling history
breakpoints #2777). Each fixed a real problem; together they left the system
with three structural faults that individually-scoped fixes cannot address.

### Fault 1: derived context is checkpointed as durable state

`KnowledgeMiddleware.before_agent()` composes the per-turn volatile context
(prior-session digest, hot memory, RAG hits, skills index, working state) as a
`HumanMessage` via `context_frame_message()` and returns it as a `messages`
state update. LangGraph's checkpointer persists it alongside operator messages.
Consequences:

- **Replay divergence.** Replaying a thread from checkpoint produces different
  context than the live run did, because the stored frame carries stale RAG hits
  and working-state snapshots from the original composition time.
- **Context growth.** Every turn adds a frame (~2-4k tokens) that is
  re-sent on every subsequent turn. A 20-turn session carries 20 frames
  (~40-80k tokens of derived context) that crowd out operator content.
- **Compaction confusion.** Auto-compaction cannot distinguish injected frames
  from operator messages without inspecting `additional_kwargs`, and the
  summarizer treats them as conversation content.

### Fault 2: memory kind conflated with storage domain

The knowledge store's `domain` column (`"hot"`, `"general"`, `"finding"`,
`"conversation"`, `"fact"`, plus freeform values like `"loop-lessons"` from
SDK callers and `doc.stem` from snapshot imports) conflates three orthogonal
concerns:

- **Kind** — what the memory IS (a fact, a preference, a finding, a session
  digest, an operator instruction).
- **Delivery policy** — WHEN it enters the prompt (always-on vs retrieved vs
  on-demand).
- **Scope** — WHERE it applies (global, per-project, per-session).

`domain="hot"` means "always inject" — a delivery policy masquerading as a
content category. `domain="finding"` means "a structured observation" — a
kind. `namespace` carries scope but is not always populated. `source_type`
carries provenance but is overloaded across write paths.

### Fault 3: two parallel context paths with no shared contract

The native runtime (`KnowledgeMiddleware` + `PromptCacheMiddleware`) and the
external runtime (`runtime/context.py` `ContextAssembler`) compose context
independently. They share `build_system_prompt()` for the stable prefix, but
their volatile-delta paths diverge:

- The native path wraps memory in `<injected_memory>` with untrusted-reference
  framing, composes `<working_state>`, uses the budgeted skill index, records
  injections to the ADR 0069 D6 log, respects incognito scoping.
- The external path (`retrieve_volatile()`, pre-#3243) uses a different format for the
  same content, has no injection logging, no working-state injection, no
  incognito support, and a different skill-index format.

### What v2 fixes

This ADR introduces the context architecture's second generation: a contract
where derived context is projected at request time (never persisted), memory
attributes are modeled as orthogonal dimensions (not overloaded columns), and
every runtime surface assembles context through one shared projection.

## Decision

### D1 — The context lifecycle has three layers, not two

The current model has a stable prefix (frozen at graph build) and a volatile
delta (recomposed per turn). V2 adds a third conceptual layer and redefines
the boundaries:

| Layer | What | Lifetime | Authority | Caching |
|-------|------|----------|-----------|---------|
| **Stable prefix** | SOUL.md persona + subagent roster + operator doctrine + tool schemas | Process (graph build) | Operator | `cache_control` breakpoint (ADR 0101 D1) |
| **Session state** | Conversation history (operator messages + assistant responses + tool results) | Session (checkpointed) | Operator + Agent | Rolling breakpoints (ADR 0101 D1) |
| **Projected context** | Prior-session digest, hot memory, RAG hits, skills index, working state | Request (recomposed) | System (derived) | Never cached; composed after the breakpoint |

The key change: the current volatile delta is checkpointed as session state
(Fault 1). V2 moves it to a third layer that is projected at request time
and never enters the checkpoint.

### D2 — Derived context is projected, never persisted

The context frame (`context_frame_message()`) is composed at request time and
delivered via `ModelRequest.override(messages=...)` (the existing LangChain
seam), not via state updates that enter the checkpointer. The projection:

1. Is composed in `before_agent()` (once per turn, as today — ADR 0101 D2).
2. Is appended to the model-visible message list via request override.
3. Is NOT returned as a `messages` state update.
4. Is NOT checkpointed.
5. IS captured by prompt observability (D5) for after-the-fact inspection.

**Migration.** Existing checkpoints with stored context frames continue to
work: the projection layer detects frames already in the checkpoint
(`is_context_frame()`) and strips them from the model-visible surface before
appending the fresh projection. Stripped frames remain in the checkpoint for
audit history but are never re-delivered to the model.

**Rollback.** If projection introduces regressions, the single change is
reverting `before_agent()` to return `{"messages": [frame]}` instead of using
the request override — the frame re-enters the checkpoint, restoring v1
behavior.

Implements: #3188.

### D3 — The system prompt is a capability-derived contract

The stable prefix is composed from declared capabilities, not ad-hoc string
concatenation. Each section of the system prompt has a named provider and a
declared contract:

| Section | Provider | Contract |
|---------|----------|----------|
| Identity | `SOUL.md` (operator-authored) | The agent's persona and domain expertise |
| Operating model | `graph/prompts.py:_OPERATING_MODEL` | Autonomous loop instructions (ADR 0079) |
| Delegation roster | `SUBAGENT_REGISTRY` | Available subagents and when to delegate |
| Tool guidance | Tool binding layer | Per-tool usage instructions |
| Operator guidelines | `graph/prompts.py` | Domain-specific behavioral rules |

The composition order is the authority order: identity wins over operating
model wins over delegation wins over tool guidance wins over guidelines.
Plugins that contribute system-prompt sections declare their section name and
position constraint (before/after).

Implements: #3190.

### D4 — Memory attributes are orthogonal dimensions

Every memory row carries these independently-valued attributes:

| Attribute | What it answers | Current column | Values |
|-----------|----------------|----------------|--------|
| **Kind** | What is this memory? | `domain` (overloaded) | `profile`, `standing`, `fact`, `decision`, `note`, `episode`, `reference`, `legacy` |
| **Provenance** | Who wrote it and how? | `source_type` | `operator`, `extracted`, `harvest`, `conversation`, `ingest`, `background_report` |
| **Trust tier** | How much should the model trust it? | Derived from `source_type` | 3 (operator), 2 (agent), 1 (external) — `knowledge/trust.py` |
| **Scope** | Where does it apply? | `namespace` | Global (empty), per-project (`project:<id>`), per-session (`session:<id>`) |
| **Delivery policy** | When does it enter the prompt? | `domain="hot"` (overloaded) | `always` (every turn), `retrieved` (RAG match), `on_demand` (tool call only) |
| **Review state** | Has an operator confirmed it? | Not modeled | `confirmed`, `pending`, `rejected` |
| **Lifecycle** | Is it still valid? | `invalidated_at` + `invalidation_reason` | Active, superseded (audit trail), expired, rejected |
| **Epoch** | What era does it belong to? | `epoch` | Freeform era marker for bulk lifecycle transitions |

**Migration.** A new `memory_kind` column and `delivery_policy` column are
added to the `chunks` table (nullable, `ALTER TABLE ADD COLUMN`). Existing
rows are backfilled from the `domain` column:

| `domain` | Condition | `memory_kind` | `delivery_policy` |
|----------|-----------|---------------|-------------------|
| `"hot"` | — | `"standing"` | `"always"` |
| `"preferences"` | — | `"profile"` | `NULL` (→ `"retrieved"`) |
| `"general"` | `source_type="conversation"` | `"note"` | `NULL` (→ `"retrieved"`) |
| `"general"` | other `source_type` | `"reference"` | `NULL` (→ `"retrieved"`) |
| `"finding"` | — | `"fact"` | `NULL` (→ `"retrieved"`) |
| `"fact"` | — | `"fact"` | `NULL` (→ `"retrieved"`) |
| `"conversation"` | — | `"note"` | `NULL` (→ `"retrieved"`) |
| Any other value | — | `"legacy"` | `NULL` (→ `"retrieved"`) |

The `domain` column is a freeform `TEXT` — callers (the SDK, snapshot
imports via `doc.stem`, operator API) may write arbitrary values. Unmapped
domains get `memory_kind="legacy"`, `delivery_policy=NULL`, which the
delivery layer treats as `"retrieved"` (included only on RAG match). The
backfill uses `source_type` as a discriminator within `domain="general"`
(agent-authored conversation notes vs imported/ingested reference) and
falls back to `"legacy"` for any unrecognized domain — no data is lost or
misclassified. The `domain` column is retained for backward compatibility
(reads from it continue to work) but new writes populate the new columns.

**Review state.** A `review_state` column (`confirmed`/`pending`/`rejected`,
default `NULL` = `pending`) enables operator confirmation of agent-derived
memories. The delivery layer can filter on review state (e.g., only inject
confirmed + pending, never rejected). Initial implementation: all existing
rows are `NULL` (pending); the console memory browser gains confirm/reject
actions.

Implements: #3072.

### D5 — Prompt observability captures the model-visible request

Every model call's actual prompt (stable prefix + session history + projected
context) is captured for after-the-fact inspection. This is not telemetry
(token counts, already in ADR 0101 D6) — it is the full prompt shape:

- What sections composed the stable prefix and their char sizes.
- Which context frames were projected and their content.
- Which checkpoint frames were stripped (D2 migration).
- The `context_sections` labels already emitted by `compose_context()`.

Capture happens at `wrap_model_call` (the existing `PromptCacheMiddleware`
boundary), persisted to the existing `TelemetryStore` as a
`prompt_snapshot` record keyed by `(session_id, turn_index, call_index)`.
The console prompt inspector renders these snapshots.

**Trajectory integration (ADR 0102).** Prompt snapshots store references
(section labels, content hashes, chunk IDs, char sizes) rather than
duplicating large text. The authoritative content lives in the ADR 0102
append-only trajectory JSONL; the snapshot links to trajectory entries by
session-scoped reference ID. Full-text capture remains available as an
explicit operator diagnostic mode (`context.capture_full_text`), bounded
and redacted, for cases where the trajectory is insufficient (e.g.,
provider-transformed prompts whose wire shape diverges from the composed
input).

Implements: #3191.

### D6 — Delivery is bounded and policy-driven

The projected context has a token budget (configurable, default ~8% of the
model's context window). Within that budget, delivery follows a priority
order:

1. **Working state** (trusted, operational — always first).
2. **Always-on memories** (`delivery_policy="always"`, the current `domain="hot"`).
3. **Skills index** (capability awareness — ADR 0060).
4. **Prior-session digest** (cross-session continuity).
5. **RAG-retrieved memories** (relevance-matched).

When the budget is exceeded, lower-priority sections are truncated or dropped
(RAG hits first, then digest, then skills overflow rows). Working state and
always-on memories are never dropped — if they alone exceed the budget, a
warning is emitted.

The incognito scoping rule (ADR 0069 D3b) is a delivery policy override:
incognito threads suppress all memory sections (1-5) but retain working state
and skills index (capability, not memory).

Implements: #3187.

### D7 — Write lifecycle: provenance, confirmation, supersession

Memory writes follow a lifecycle:

1. **Creation.** Every write stamps `source_type` (provenance), `kind`,
   `delivery_policy`, `scope` (namespace), and `review_state`.
2. **Confirmation.** Agent-derived memories (`trust_tier=2`) start as
   `review_state="pending"`. The operator can confirm or reject them. The
   delivery layer respects review state per D6.
3. **Supersession.** When a newer memory on the same topic is written, the
   older one is superseded (`invalidated_at` stamped, `invalidation_reason`
   set) — never deleted. The superseded row remains for audit history and is
   excluded from delivery but available via `memory_recall` with a
   `include_superseded=True` flag.
4. **Expiration.** Memories can carry an optional `expires_at` timestamp.
   The delivery layer excludes expired memories; the store retains them.

The existing `invalidated_at` + `invalidation_reason` columns already model
supersession (ADR 0069 D9). This decision formalizes the lifecycle and adds
confirmation and expiration.

Implements: #3185.

*Amendment (#3246):* tier 1 (ingested / external) and unknown source types also
start `review_state="pending"`, not only the agent tier — a write path that
doesn't identify itself gets the least trust, not the benefit of the doubt.
SDK and eval writes with no `source_type` are tier 1. Rows that predate the
rule are stamped by a one-shot backfill on first open (its own `_kb_meta`
marker, `review_state_backfill`, separate from the D4 pass).

### D8 — Surface parity: one projection for every runtime

The native runtime (`KnowledgeMiddleware`) and external runtimes
(`runtime/context.py`) share one projection function. The refactored
`compose_context()` becomes a standalone function that both paths call:

```
compose_projected_context(
    query: str,
    knowledge_store,
    skills_index,
    state: dict,
    *,
    incognito: bool = False,
    budget_tokens: int = ...,
    record: bool = True,
) -> ProjectedContext
```

`ProjectedContext` replaces the current `{"context": str, "context_sections":
list}` dict with a typed dataclass carrying the composed text, section
metadata, and injection record. Both `KnowledgeMiddleware.before_agent()` and
`ContextAssembler.assemble()` call it. The external path gains:

- `<injected_memory>` untrusted-reference envelope (currently native-only).
- `<working_state>` injection (currently native-only).
- Budgeted skill index (currently native-only).
- Injection logging (ADR 0069 D6, currently native-only).
- Incognito scoping (currently native-only).

Implements: #3189.

### D9 — Prior-session digest is evaluated, not blindly injected

The prior-session digest (the `<prior_sessions>` block) is currently a fixed
rendering of the N newest session summary files, TTL-cached for 60s. V2
changes:

- **Relevance gating.** The digest is only injected when the current
  session's topic has a non-trivial overlap with a prior session (measured by
  the same FTS5/hybrid search already in the RAG path, not by an LLM judge —
  ADR 0069's evidence section showed LLM freshness judges are unreliable).
- **Attribution.** Each prior-session entry carries its session ID and
  timestamp, already tracked in `_prior_sessions_ids`.
- **Budget participation.** The digest competes for the D6 budget alongside
  other projected sections, rather than having a separate hardcoded cap.

The goal-turn suppression (current: digest suppressed on autonomous goal
turns) is preserved as a delivery-policy override.

Implements: #3186.

## Authority and persistence boundaries

How context behaves across the five runtime modes:

| Mode | Stable prefix | Session state | Projected context | Memory writes | Memory reads |
|------|--------------|---------------|-------------------|---------------|--------------|
| **Native** (LangGraph) | Full SOUL + roster + doctrine | Checkpointed (messages channel) | Composed per turn by `compose_projected_context()` | All write paths available | Full: always-on + RAG + digest |
| **ACP** (external runtime) | Same via `build_stable_prefix()` | External runtime's own storage | Same via `compose_projected_context()` (D8) | Via operator MCP tool bridge | Same (D8 parity) |
| **Subagent** (`task()` delegation) | Subagent-specific prompt (`build_subagent_prompt()`), returned verbatim — no SOUL, no roster | Own checkpoint (scoped thread) | **None** — the subagent stack has no `KnowledgeMiddleware`, so nothing is projected: no `<injected_memory>` framing, skills index, working state, incognito rule or D6 budget. Memory is reachable only as an explicit `memory_recall` tool where the subagent's allowlist grants it (pull-on-demand). Giving subagents a defined context contract is tracked on #3189 | Writes scoped to parent's store | Reads from parent's store |
| **Autonomous** (goal-driven turn) | Same as native | Same session checkpoint | Digest SUPPRESSED (goal-turn override); working state + RAG active | All write paths | No digest; RAG + always-on |
| **Incognito** (ADR 0069 D3b) | Same as native | Checkpointed but memory-isolated | Working state + skills only (no memory injection) | NO writes to knowledge store | Skills index only (no memory) |

## Migration

### Phase 0: characterization (this PR)

Characterization tests capture current behavior as a regression baseline.
No runtime changes.

### Phase 1: projection without persistence (#3188)

`before_agent()` switches from state-update delivery to request-override
delivery. Context frames stop entering the checkpoint. Existing checkpoint
frames are detected and stripped at projection time. Rollback: revert the
delivery path (one function change).

### Phase 2: typed memory columns (#3072)

`memory_kind`, `delivery_policy`, `review_state` columns added to `chunks`.
Backfill migration for existing rows. `domain` column retained read-only.
Rollback: the new columns are nullable and additive; removing the backfill
query and column reads restores v1 behavior.

### Phase 3: prompt observability (#3191)

`prompt_snapshot` records added to `TelemetryStore`. `PromptCacheMiddleware`
captures the full prompt shape at `wrap_model_call`. Console inspector
renders snapshots. Rollback: stop writing snapshots (the table is additive).

### Phase 4: shared projection (#3189, #3190)

`compose_projected_context()` extracted as standalone function. Both native
and external runtimes call it. System prompt composition formalized.
Rollback: revert to separate composition paths.

Shipped in #3243 (D8). `retrieve_volatile()` was removed; `runtime/context.py`
calls `compose_projected_context()`.

### Phase 5: delivery policy + digest evaluation (#3187, #3186, #3185)

Budget-driven delivery, relevance-gated digest, write lifecycle
(confirmation + expiration). Rollback: revert to unbounded injection and
unconditional digest.

Shipped:

- D7 in #3246: creation stamps via the trust tier, `set_review_state` +
  `POST /api/memory/chunks/{id}/review`, the `superseded_by:<id>` chain with
  insert-then-invalidate, `expires_in_days` on `memory_ingest`.
- D6 in #3247: `context.budget_pct` (default 8%, floored at 16k chars, `0` =
  unbounded), the fixed priority/shed order in `graph/projection.py`, always-on
  selected by `delivery_policy="always"`, `deliverable=True` (rejected/expired
  excluded) on every store's `list_chunks`/`search`, and the budget summary on
  the prompt preview API. The `delivery_order` config key named under
  Compatibility was not added — the order is fixed by this decision.
- D9 in #3252: active-session exclusion (a thread's own summary is never a
  "prior" session), `context.prior_sessions: newest|relevant|off` (default
  `newest` pending the #3186 eval; `relevant` gates on the session-search FTS
  index, no LLM judge), and per-entry digest shedding under the D6 budget
  (`DigestResult` entries, dropped oldest / lowest-rank first).

### Compatibility behavior

- **Existing checkpoints.** Stored context frames are stripped from the
  model-visible surface but retained in the checkpoint. A thread started on
  v1 and continued on v2 sees fresh projected context, not stale stored
  frames.
- **Existing memory rows.** The `domain` column continues to work for reads.
  New writes populate both `domain` (for backward compat) and the new typed
  columns. A v2 reader falling back to v1 storage sees the same behavior.
- **Plugin SDK.** `knowledge_store.add_chunk()` accepts the new columns as
  optional kwargs. Plugins that do not pass them get defaults (`kind=NULL`,
  `delivery_policy=NULL` = "retrieved"). Existing plugin writes are
  unaffected.
- **Config.** New config keys (`context.budget_pct`, `context.delivery_order`)
  have defaults matching current behavior. No existing config is invalidated.

## Non-goals

- **Embedding/vector overhaul.** The hybrid FTS5+vector store (ADR 0041,
  `HybridKnowledgeStore`) is orthogonal to the context lifecycle. V2 does not
  change retrieval ranking.
- **Cross-instance memory.** Memory sharing between instances (fleet commons,
  ADR 0041) is a separate concern. V2's scope model (`namespace`) is
  compatible with it but does not implement it.
- **Real tokenizer.** Token estimation continues to use the chars//4 heuristic
  (ADR 0101's reasoning applies: precision at decision thresholds, not
  billing).
- **Graph-level context windowing.** Derived-view compaction via
  `ModelRequest.override(messages=...)` for the full session history (deferred
  in ADR 0101) remains deferred — D2's projection is the first use of the
  same seam, scoped to injected context only.
- **Memory UI redesign.** The console memory browser's UX is out of scope.
  V2 adds the data model (review state, typed columns); the browser's
  rendering of those attributes is a separate effort.

## Consequences

- **Context growth is bounded.** Projected context occupies a fixed budget
  per request, regardless of session length. A 20-turn session no longer
  accumulates 20 context frames in the checkpoint.
- **Replay is deterministic.** Replaying a checkpoint produces the same
  operator messages + fresh projected context, not stale stored context.
- **Memory is queryable by kind.** Operators and the delivery layer can
  filter by memory kind, delivery policy, review state, and scope
  independently.
- **Runtime parity.** External runtimes (ACP, future runtimes) get the same
  context quality as the native LangGraph loop.
- **One-time migration cost.** Phase 2's backfill migration runs on existing
  knowledge stores. The `ALTER TABLE ADD COLUMN` is safe (nullable, no
  default rewrite on SQLite), but the backfill query touches every row once.
- **Prompt snapshot storage.** Phase 3 adds per-call prompt snapshots to the
  telemetry store. Storage is bounded by the existing telemetry retention
  policy.

## References

- ADR 0021 — Agent memory architecture (extract, don't dump)
- ADR 0033 — Pluggable agent runtime / ACP (context plane contract)
- ADR 0041 — Workspaces and tiered stores
- ADR 0060 — Skill progressive disclosure
- ADR 0069 — Memory delivery layer (attribution, provenance, trust tiers)
- ADR 0079 — Autonomous operating model (working state)
- ADR 0101 — Context lifecycle: log, surface, pressure (Endless Context)
- ADR 0102 — The trajectory: session log and derived surface
- Tracker: #3184 (umbrella), #3188 (D2), #3191 (D5), #3190 (D3), #3072 (D4),
  #3187 (D6), #3185 (D7), #3189 (D8), #3186 (D9)
