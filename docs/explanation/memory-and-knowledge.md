# Memory & the knowledge store

protoAgent has a single durable **knowledge store**, a set of conventions for
*what* goes in it, and — just as deliberately — a **delivery layer** that
decides *what comes back out into the prompt*, under what framing, with what
audit trail. This page explains the whole pipeline: the store, the three kinds
of memory, the write paths, the per-turn injection, and the forensics.

Two ADRs own the design:

- [ADR 0021](../adr/0021-agent-memory-architecture.md) — what the agent
  *stores* ("extract, don't dump").
- [ADR 0069](../adr/0069-memory-delivery-layer.md) — how memory is *delivered*
  into the prompt (attributed digest, untrusted-reference framing, provenance,
  trust tiers, injection forensics).

## The store

`knowledge/store.py` is a SQLite database with **FTS5 full-text search** (with a
`LIKE` fallback when FTS5 isn't compiled in). One `chunks` table holds everything
the agent knows; rows are distinguished by a few columns:

| Column | Meaning |
|---|---|
| `domain` | the bucket — `fact`, `conversation`, `hot`, `finding`, or anything a tool sets (`preferences`, `context`, …) |
| `finding_type` | sub-type within a domain (e.g. `fact`, `conversation`) |
| `namespace` | optional per-project / per-owner scope (ADR 0021) — a *filter* for multi-project forks, never required |
| `source` / `source_type` | provenance: for conversation-derived rows (harvest summaries, extracted facts, background reports) `source` is the **originating session/thread id** (ADR 0069 D5); for ingested content it is the **document origin** (URL/path); other writes may leave it unset or stamp the writing surface (e.g. `console`). `source_type` names the write path and maps onto a [trust tier](#trust-tiers) |
| `created_at` / `invalidated_at` | when the row was stored, and — for superseded facts — when a newer revision replaced it (ADR 0069 D9; retrieval excludes invalidated rows by default) |
| `memory_kind` / `subject` / `review_state` / `expires_at` | typed memory (#3072, [ADR 0108 D4](../adr/0108-context-architecture-v2.md)): what the chunk *is* (`profile`, `standing`, `fact`, `decision`, `note`, `episode`, `reference`, `legacy`), what it's about, whether an operator confirmed it, and when it lapses. `NULL` on untyped rows |
| `delivery_policy` | *when* the chunk enters the prompt (ADR 0108 D4): `always` (every turn — what `domain="hot"` has always meant; a hot write is stamped `always` automatically), `retrieved` (on a RAG match — the `NULL` default), `on_demand` (only via `memory_recall`). Rows that predate the column were classified once from `domain` on the first open after the upgrade |
| `heading`, `content` | the chunk itself |

## Three kinds of memory

protoAgent follows the standard semantic / episodic / procedural split, mapped
onto primitives it already has:

- **Semantic** — discrete, durable **facts** (`domain="fact"`). "The user deploys
  on Tuesdays." Extracted by the session-end pass; queryable like any chunk.
- **Episodic** — two layers. **Session summaries**: every session persists a
  reasoning-stripped JSON summary to disk, which later threads see as a one-line
  digest entry (below). **Conversation summaries** (`domain="conversation"`): a
  retired thread is summarized into searchable store chunks.
- **Procedural** — **Playbooks / skills** (`skills.db`, a separate FTS5 index).
  Methodology the agent retrieves but never "runs". See [Skills](../guides/skills.md).

## Write paths

1. **Memory tools** — the agent calls `memory_ingest` to record a fact the user
   shared, `memory_recall` to search durable knowledge, and `session_search` /
   `recall_session` to find prior conversations by content or id. See
   [Starter tools](../reference/starter-tools.md).
2. **Session summaries** — `SessionSummaryMiddleware` writes a per-session JSON
   summary (messages, top tool calls, final output — all reasoning-stripped) to
   the session-memory dir on each terminal turn, atomically (temp file →
   rename, so a crash never leaves a partial file). It skips incognito threads,
   `background:*` worker sessions ([ADR 0070](../adr/0070-background-results-push-resume.md):
   the worker's transcript is disposable — its *report* is delivered and indexed
   to the origin session instead), and turns with no resolvable session identity
   (no more pooled `unknown.json`).
3. **Harvest on retirement** — when a chat thread is retired (aged out by the
   checkpoint pruner, or deleted), `graph/conversation_harvest.py` runs a single
   **session-end pass** (the cheap `routing.aux_model`): it stores an episodic
   *summary* (`source_type="harvest"`) and, when `knowledge.facts` is on,
   **extracts durable facts** (`source_type="extracted"`). This is *extract,
   don't dump* — it never stores raw turns, and the same no-trail rules hold at
   retirement: incognito and `background:*` threads are never harvested. Every
   row carries the originating thread id in `source`, so recall and audit can
   always answer "where did this come from".

### The reasoning guardrail

The agent reasons natively — on the gateway's `reasoning_content` channel, not in
the answer text (see [model output](output-protocol.md)). As a defense-in-depth
guardrail, `add_chunk` **strips any leaked `<scratch_pad>`/`<think>` from every
write** — so reasoning a provider leaks into content can never reach the store (and
never gets recycled into a later prompt via retrieval). A chunk that is *only*
reasoning is dropped, not stored empty. The session-summary path applies the
same strip on write *and* on read.

### Facts: dedup + supersede, deterministically

The fact extractor consolidates before it inserts (`graph/memory_facts.py`):

- **Duplicate** — a new fact with token-set (Jaccard) overlap **≥ 0.85**
  against an existing fact in the same namespace is skipped.
- **Revision** — overlap in the **0.6–0.85 band** means same subject, changed
  details: the old row is stamped `invalidated_at` and the new row inserted
  (**supersede, don't delete** — ADR 0069 D9). History is kept for audit;
  retrieval excludes invalidated rows by default.

Both checks are pure token math — never an LLM freshness judgment. LLMs
demonstrably can't self-adjudicate staleness, so recency is handled with
explicit signals at retrieval time instead: every injected hit carries its
stored date, and the model weighs freshness from timestamps it can see.

### Write lifecycle (ADR 0108 D7)

Every write is typed on the way in, and every memory has a lifecycle after it:

1. **Creation stamps.** `add_chunk` fills what the caller left blank:
   `memory_kind` from the domain (+ source type — the same table the one-shot
   D4 backfill used), `delivery_policy` (`always` for `domain="hot"`, otherwise
   retrieved) and `review_state` from **who wrote it**: an operator surface
   ([trust tier](#trust-tiers) 3 — the console's Knowledge and Memory routes)
   starts `confirmed`; the agent's own writes (tier 2 — `memory_ingest`, fact
   extraction, harvest) and ingested / third-party content (tier 1 — document
   ingests, snapshot seeds, and any write with no `source_type` at all, which is
   how plugin SDK and eval writes arrive) start `pending`. Explicit values
   always win; `subject` and `expires_at` are never guessed. Rows that predate
   this rule are stamped the same way by a one-shot pass on the first open after
   upgrading (its own `_kb_meta` marker, separate from the D4 pass), so every
   row carries a verdict and filters compare the column directly. A
   `memory_ingest` write also records **which session it happened in**, in the
   `source` column — the same machine-readable link the harvest path already
   wrote there (ADR 0069 D5), so `memory_recall` cites `src:` for an
   agent-remembered fact just as it does for a harvested one. The session id is
   read from the graph state the tool was invoked with, never from the model:
   provenance the model could set is provenance it could forge.
2. **Confirmation.** The operator confirms, rejects, or re-opens a row with
   `POST /api/memory/chunks/{id}/review` and a body of `{"state": "confirmed" |
   "rejected" | "pending"}` — the Memory inspector's verdict. Rejecting never
   deletes: the row keeps its content and history and simply stops being
   deliverable (D6, #3187, filters on the verdict). The agent has no confirm
   tool; `memory_list(review_state="pending")` shows it what is still waiting on
   the operator. An operator *edit* (`PUT /api/knowledge/chunks/{id}`) is an
   assertion about the content, so the new revision is `confirmed` and keeps the
   row's kind, subject, policy, expiry and scope — except a `rejected` row stays
   rejected (re-opening is the review route's job). Promoting a row into the
   commons confirms the commons copy by construction.
3. **Supersession chain.** A revision invalidates the old row *after* the new
   one has landed, stamping `invalidation_reason="superseded_by:<new id>"` — a
   queryable audit chain, never a delete, and never reaped by the bulk-delete
   grace sweep (which matches its own marker only). A failed insert therefore
   never loses the old fact; the reverse (the insert landed, the invalidation
   didn't) is logged and leaves both rows valid until the next revision. On a
   layered store only private rows are ever superseded — a commons match still
   dedups by content but is never invalidated. `memory_recall(include_superseded=True)`
   surfaces that history tagged `[superseded by #<id>]` (or `[superseded]` for a
   pre-chain row).
4. **Expiration.** `memory_ingest(expires_in_days=N)` (1–3650) stamps
   `expires_at`; the row is kept but leaves delivery once it lapses.
   `memory_list` shows `expires=YYYY-MM-DD` on such rows.

Plugins get the same lifecycle through `sdk.knowledge_add(review_state=,
expires_at=)`; a plugin write that sets neither starts `pending`, like any
other non-operator write.

## Delivery: what enters the prompt (ADR 0069)

`KnowledgeMiddleware` runs before each model call and assembles the injected
context. Everything memory-derived rides inside **one `<injected_memory>`
envelope** whose header states it is *reference data* — possibly stale, possibly
third-party — never instructions and never part of the current conversation
(the OWASP ASI06 memory-poisoning posture: reduce memory's authority at the
prompt layer, don't just hope the store stays clean). Three parts, in order:

1. **The prior-sessions digest.** One **attributed line per session** — id ·
   timestamp · surface (chat/a2a/…) · topic · message count — under a
   ~2 000-token cap, behind a framing header that says these are *other,
   separate* sessions. The topic derives from the first *user* message only
   (no assistant text — that's the identity confusion + poisoning surface).
   Which sessions the digest lists is a policy, `context.prior_sessions`
   ([ADR 0108 D9](../adr/0108-context-architecture-v2.md)): `newest` (default)
   takes the newest 10 summaries; `relevant` takes only sessions whose content
   matches the turn's query (the session-search FTS index, best match first,
   falling back to `newest` on an empty query, a build without FTS5, or zero
   matches); `off` injects no automatic digest at all — `session_search` /
   `recall_session` remain the on-demand path. Whatever the policy, the
   **active session's own summary is never injected as a "prior" session**
   (it is the newest file on disk from turn 2 on — the loader excludes it
   before the newest-N cut, so the digest refills instead of running short).
   Under `newest` the shared cache holds one *more* summary than the digest
   shows, refreshed on a 60 s TTL; the per-session exclusion, the length trim
   and the token trim then run per call, so dropping the caller's own summary
   refills the freed slot rather than shortening its digest. `relevant` reads fresh
   (query-dependent by definition — one index sync, one FTS query, up to N
   small JSON reads per turn). The digest is suppressed on goal-driven turns.
   The full summary of any listed session is one tool call away with
   `recall_session(session_id)`; when the id is unknown, `session_search(query)`
   searches reasoning-stripped, credential-redacted transcript content in a
   lazy FTS5 index and returns ids to expand.
2. **Always-on memory ("hot").** Chunks with `delivery_policy="always"` are
   always-on operator facts: the newest 100 under a 6 000-char budget inject
   **every turn**, loaded fresh per turn so a just-added fact is seen
   immediately. Always-on is a *policy*, not a domain
   ([ADR 0108 D4 + D6](../adr/0108-context-architecture-v2.md)): every
   `domain="hot"` write is stamped with it whatever the caller said, so the
   legacy hot domain still works, and a row on any other domain (say a
   `preferences` fact) can be pinned always-on the same way. Rows an operator
   has rejected (`review_state="rejected"`) or that have passed their
   `expires_at` never deliver, whatever their policy. Because always-on makes
   a silent write the highest-leverage poisoning move available, every
   always-on write — agent tool, console route, or plugin — emits a
   `memory.hot_written` bus event, and an optional gate
   (`knowledge.hot_write_confirm`) makes the agent's own write paths
   (`memory_ingest`, `knowledge_ingest`) refuse `domain="hot"` and
   `delivery_policy="always"` alike, reserving always-on promotion for
   operator surfaces. The console's Memory → Hot memory list shows exactly the
   always-on set the reader selects.
3. **RAG hits.** The store is searched with the last user message and the
   top-k results (default 10) inject, each line ending with its stored date and
   trust label — `(stored 2026-07-01; trust: agent)`. Two policies shape the
   list: **namespace scoping** (`knowledge.inject_namespaces` restricts what may
   enter the prompt *unasked*; tool-driven `memory_recall` stays unscoped) and
   **trust tiers** (below).

The always-on `<available_skills>` index stays **outside** the envelope — it is
capability, not memory ([ADR 0060](../adr/0060-skill-progressive-disclosure.md)).

### One projection for every runtime (ADR 0108 D8)

The composition above is a standalone function —
`graph.projection.compose_projected_context()` — not middleware logic. The
native loop's `KnowledgeMiddleware` calls it with the last user message, the
thread's incognito flag, and its TTL-cached digest; an external runtime
(`runtime/context.py`, the ACP path) calls the same function from
`assemble_context()`. So a brain outside the graph is fed the same
`<injected_memory>` envelope, hot memory, trust-ranked hits, budgeted skill
index, and `<working_state>` the native loop injects, with the incognito rule
and the injection log applied identically — and the delivery knobs come off the
same config (`ProjectionOptions.from_config`). The middleware keeps only what is
graph-specific: the turn-entry guard, the digest cache, and the ephemeral
delivery ([ADR 0108](../adr/0108-context-architecture-v2.md) D2). The result is
a typed `ProjectedContext` — text, per-section labels, the injected ids, and
the sources that fed it.

### Delivery budget and priority (ADR 0108 D6)

The projection is **bounded**: it may use at most `context.budget_pct` of the
model's context window (default 8%, chars//4 — the same token heuristic the
rest of the runtime uses), and never less than **16 000 chars** — roughly the
always-on cap (6 000) plus the digest cap (~2 000 tokens), so a small-window
model keeps its standing context whole and sheds only what lies beyond it. The
ceiling is derived from the window the gateway reports for the model; no
window (logged once — the knob is inert), or `budget_pct: 0`, means unbounded.
The stable prompt is not part of it — only what is injected on top per turn.

The ceiling follows **the turn's** model, not the configured default: a chat tab
switched to a smaller model gets that model's allowance (and its skill-index
cap), so it can't carry a large model's budget into a small window. A model the
gateway doesn't report falls back to the configured default's window.

Within the budget, the parts fill in a fixed priority (highest first):

1. **Working state** — the agent's own live commitments (trusted, operational).
2. **Always-on memory** — `delivery_policy="always"`.
3. **The skill index** — capability awareness.
4. **The prior-session digest** — cross-session continuity.
5. **RAG hits** — relevance-matched knowledge.

Over budget, the lowest-priority parts shed first, each step re-measured so
nothing is cut mid-line: RAG hits go one whole hit at a time from the
lowest-ranked end, then the prior-session digest one **entry** at a time from
the end (oldest under `newest`, lowest-rank under `relevant` — ADR 0108 D9;
the section drops when none remain), then the skill index gives up
descriptions one row at a time down to its identity floor — every skill's name
stays listed ([ADR 0060](../adr/0060-skill-progressive-disclosure.md), #2867).
Working state and always-on memory are **never shed**: if they alone exceed
the budget they are delivered anyway and a warning names the sizes (once per
distinct standing-context size), because a silently missing standing
instruction is worse than an oversized prompt. The order is fixed by the ADR —
there is no `delivery_order` knob to reorder it.

What was shed is visible: the prompt **preview API** (`GET /api/prompts/preview`)
carries the `budget` summary — ceiling, chars used, and an `overflow` list
(label, items and chars dropped per part) — and marks each shed section
`truncated`; the console inspector renders both in a follow-up. The injection
log records the ids that actually **entered** the turn, never those merely
retrieved. With the default 8% on a 128k-window model the ceiling is ~10k
tokens — more than a typical turn injects (6 000 chars of always-on memory, a
~2 000-token digest, ten hits, a 2%-of-window skill index) — so nothing is
shed there until you lower it; on a 32k window or smaller the 16k-char floor is
the budget, so RAG hits and skill descriptions beyond it now shed where they
used to be unbounded.

### Trust tiers

Every chunk's `source_type` ranks into three deterministic tiers
(`knowledge/trust.py` — a code-level map, not config):

| Tier | Label | Who wrote it |
|---|---|---|
| 3 | `operator` | the operator, deliberately, through a console surface |
| 2 | `agent` | derived from conversation: extracted facts, harvest summaries, `memory_ingest`, indexed background reports |
| 1 | `external` | ingested third-party content (web, YouTube, PDF, media) — **and any unknown/unstamped source** |

Auto-injected RAG hits are stable-sorted by tier after retrieval — an external
hit never outranks an operator- or agent-authored one, while relevance order is
preserved within a tier. A floor (`knowledge.inject_min_trust`) can exclude low
tiers from auto-injection entirely; excluded content stays reachable on demand
via `memory_recall`, tier visible. The knobs, with worked examples:
[Tune the knowledge store → Memory delivery controls](../guides/knowledge.md#memory-delivery-controls-adr-0069).

### Incognito threads

A thread flagged incognito leaves no memory trail and reads none in: no session
summary is written, the retire-time harvest skips it, and the digest / hot
memory / RAG injection is skipped for its turns. The skill index still injects
— capability, not memory. (How to flag a thread — slash command, API field, A2A
metadata: [the guide](../guides/knowledge.md#incognito-threads).)

## Forensics: the injection log

Every model call that had memory injected appends one row to an
instance-scoped SQLite log (`<instance_root>/memory-injections.db`): which
digest sessions, which hot chunk ids, which RAG chunk ids entered the prompt,
for which session, at what approximate token cost. Served at
`GET /api/memory/injections`. This closes the audit chain — store row → source
session → the exact turns it was injected into — which is what turns
SpAIware-class memory poisoning from undetectable into greppable.

The console's **Memory** view is the inspect-audit-prune surface built on it,
in three tabs:

- **Sessions** — the summary files behind the digest (rows reuse the exact
  digest derivation, so what you see is what the agent is told); view/delete,
  jump to a session's injections.
- **Hot memory** — the always-on chunks; view/edit/delete.
- **Injections** — the per-turn record, filterable by session.

The broader store is browsable under **Knowledge → Store**.

## Semantic recall (embeddings)

The store is keyword-only FTS5 **by default** (`knowledge.embeddings: false`):
out of the box the app must not depend on an optional gateway route — a gateway
without a working embedding model turned every turn's recall into a stall. Once
your gateway serves an embedding model, opt in and the store becomes
`HybridKnowledgeStore`: FTS5 keyword search fused with **vector similarity**
via Reciprocal Rank Fusion, so lexical *and* semantic hits reinforce each other
(keyword-only misses paraphrases — *"how do I ship a build?"* won't match a
stored *"the release pipeline is manual via workflow_dispatch"*). An embedding
circuit breaker falls back to FTS5 on an embedding outage — quality degrades,
availability never does.

```yaml
knowledge:
  embeddings: true             # opt-in: hybrid semantic + keyword
  embed_model: qwen3-embedding # MUST be a model your gateway serves (see below)
```

::: warning The embed model is gateway-specific
`embed_model` must name a model your [LiteLLM gateway](litellm-gateway.md)
actually serves — it is **not** the chat model. The default `qwen3-embedding`
suits the protoLabs gateway; for a local Ollama gateway set something it serves
(e.g. `nomic-embed-text`). Check `GET /v1/models` for what your key can access. With a
wrong model every embed call 401/404s, the breaker opens, and you silently get
keyword-only search.
:::

Embeddings are routed through the same gateway as the chat model
(`graph.llm.create_embed_fn`), sending the **raw string** (not client-side
tokenized arrays) so OpenAI-compatible gateways accept the request.

## Configuration

All under the `knowledge:` block (see [Configuration](../reference/configuration.md);
tuning guidance in [Tune the knowledge store](../guides/knowledge.md)):

| Key | Default | Effect |
|---|---|---|
| `db_path` | `/sandbox/knowledge/agent.db` | store location (instance-scoped) |
| `embeddings` | `false` | opt-in hybrid semantic + keyword search (vs keyword-only) |
| `embed_model` | `qwen3-embedding` | gateway embedding model (set per your gateway) |
| `facts` | `true` | extract semantic facts during the session-end pass |
| `top_k` | `10` | how many RAG chunks inject per turn |
| `inject_namespaces` | `[]` | namespaces allowed to auto-inject (empty = unfiltered; `""` matches un-namespaced) |
| `inject_min_trust` | `1` | trust floor for auto-injection: 1 = down-weight only, 2 = drop external, 3 = operator-only |
| `hot_write_confirm` | `false` | when on, the agent's `memory_ingest` and `knowledge_ingest` refuse always-on writes (`domain="hot"` or `delivery_policy="always"`) |
| `scope` | `scoped` | tier ([ADR 0041](../adr/0041-workspaces-and-tiered-stores.md)): `scoped` (private) · `shared` (host commons) · `layered` (read commons ∪ private, write private). See [Tune the knowledge store → Sharing across a fleet](../guides/knowledge.md#sharing-knowledge-across-a-fleet-the-commons) |
| `middleware.knowledge` | `true` | turn the whole subsystem on/off |
| `context.budget_pct` | `8` | (its own `context:` block) the projected-context ceiling as a % of the model window ([D6](#delivery-budget-and-priority-adr-0108-d6)); `0` = unbounded |

Three environment knobs override paths and persistence directly:

| Env var | Effect |
|---|---|
| `PROTOAGENT_DISABLE_MEMORY` | `1`/`true`/`yes` disables session-summary persistence entirely |
| `MEMORY_PATH` | session-summary dir (default: the instance `memory/` store) |
| `PROTOAGENT_INJECTION_LOG` | injection-log DB path (default: `<instance_root>/memory-injections.db`) |

Tip: enabling embeddings is measurable — add a recall eval and compare keyword vs
hybrid via `evals.sweep`. See [Eval your fork](../guides/evals.md).

## See also

- [ADR 0021 — Agent memory: extract, don't dump](../adr/0021-agent-memory-architecture.md)
- [ADR 0069 — Memory delivery layer](../adr/0069-memory-delivery-layer.md) — digest, framing, provenance, trust tiers, injection record
- [ADR 0070 — Background results](../adr/0070-background-results-push-resume.md) — why `background:*` workers leave no summary trail
- [Tune the knowledge store](../guides/knowledge.md) — the tuning knobs + the delivery-control recipes
- [ADR 0041 — Workspaces & tiered stores](../adr/0041-workspaces-and-tiered-stores.md) — the private/commons tiering behind `knowledge.scope`
- [Run a fleet](../guides/fleet.md) — sharing a knowledge commons across many agents on one host
- [Prompt contracts](prompt-contracts.md) — the stable prompt this projected context is deliberately kept out of, and the size ceilings that keep it cacheable
- [Model output](output-protocol.md) — native reasoning + the leaked-reasoning guard this enforces
- [Skills](../guides/skills.md) — procedural memory (Playbooks)
- [Starter tools](../reference/starter-tools.md) — the `memory_*` tools
