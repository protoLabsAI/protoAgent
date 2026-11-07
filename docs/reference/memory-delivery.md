# Memory storage and delivery internals

<span id="memory-storage-and-delivery-internals"></span>

For the user-facing model and controls, start with [Memory and knowledge](/explanation/memory-and-knowledge). This page describes storage, provenance, and prompt delivery; settings are owned by [Knowledge reference](/reference/knowledge).

## The stores

[ADR 0125](/adr/0125-memory-knowledge-history-separation) separates three layers:

| Layer | Storage | Tools |
|---|---|---|
| Memory — durable preferences, standing rules, facts and decisions | Core SQLite `memories` table (`knowledge/memories.py`), FTS5 plus optional vectors | `memory_save`, `memory_search`, `memory_list`, `memory_forget` |
| Knowledge — ingested reference documents | `chunks` collections through the knowledge backend; private and commons tiers | `knowledge_ingest`, `knowledge_search` |
| History — archives, session summaries and background reports | Core SQLite `history` table; no embeddings | `session_search`, `recall_session` |

Memory has no domain. Knowledge collections group documents, such as a set of rulebooks
or project manuals; `knowledge_ingest(collection="manuals")` writes that collection and
`knowledge_search(query="deployment", collection="manuals")` searches it. The backend
still uses `domain` as its collection filter internally. History content is retrieved on
demand, not automatically injected as memory. Skills remain a separate capability index;
see [Skills](/guides/skills).

### Memory fields

| Field | Meaning |
|---|---|
| `kind` | `profile`, `standing`, `fact` or `decision`; `memory_save` defaults to `fact` |
| `subject` | The entity the memory is about; optional, never guessed by the store |
| `heading` / `content` | Optional short label and the durable text |
| `delivery` | `always` (every turn), `retrieved` (relevance-based, default), or `on_demand` (explicit `memory_search`) |
| `review_state` | `pending`, `confirmed` or `rejected`; the operator owns the verdict |
| `provenance` | `operator`, `agent` or `extracted` — who supplied the memory |
| `source_session` | Originating session, stamped from injected state for agent tool writes |
| `scope` | Optional namespace |
| `valid_from` / `superseded_by` | Validity start and the replacement memory ID, if superseded |
| `expires_at` | Optional expiry; the row remains stored after it leaves delivery |
| `created_at` / `updated_at` | Storage and revision timestamps |

## Write paths

1. **Deliberate memory** — `memory_save` records text already in hand. Save durable
   preferences and decisions; keep release logs, PR status and card progress on their
   board or issue. Agent writes carry `provenance="agent"`, start pending and record
   `source_session` from graph state, never from a model-supplied argument.
2. **Reference material** — `knowledge_ingest` fetches and extracts URLs or files into
   `chunks`, grouped by `collection`. These are documents, not personal memories.
3. **Conversation history** — session summaries, retirement summaries and background
   reports support `session_search` and `recall_session`. Session summaries strip
   reasoning, skip incognito and disposable `background:*` worker sessions, and keep
   the originating session identity. Durable fact extraction is controlled by
   `knowledge.facts`; extracted memory provenance is distinct from agent-authored memory.

### The reasoning guardrail

The agent reasons on the gateway's `reasoning_content` channel, not in answer text
([model output](/explanation/output-protocol)). Document `add_chunk` strips leaked
`<scratch_pad>`/`<think>` blocks and drops reasoning-only chunks. The session-summary
path strips reasoning on write and read.

### Memory lifecycle

Operator writes default to `review_state="confirmed"`; agent and extracted writes
start `pending`. Pending private memories remain eligible for delivery. Rejected,
expired and invalidated rows are excluded from search and delivery. Superseded rows
remain for audit and are excluded by default; `memory_search(include_superseded=True)`
can return them tagged `[superseded]`, without overriding rejection or expiry.

`memory_list(review_state="pending")` lists eligible memories awaiting review. Its final
line reports counts per kind and a total for the store, independent of the listing
filters. The agent has no confirmation tool; the operator reviews memories in the
Memory inspector. The review API uses `POST /api/memory/chunks/memory:<id>/review`
with `{"state": "confirmed" | "rejected" | "pending"}`; bare numeric handles on that route
address knowledge chunks instead. `memory_save(expires_in_days=N)` accepts 1–3650 and
stamps `expires_at`.
`memory_forget(memory_id)` permanently deletes one memory; it does not delete a knowledge
chunk with the same numeric ID.

## Renamed tools

**Migrating from memory_ingest/memory_recall:** this is a hard rename with no aliases.
Update tool allowlists, prompts and callers:

- `memory_ingest` → `memory_save`; replace domain-based classification with `kind` and
  `subject`, and use `delivery` instead of `delivery_policy`.
- `memory_recall` → `memory_search` for memory only; use `knowledge_search(collection=)`
  for documents and session tools for history.
- `forget_memory` → `memory_forget`, taking `memory_id` instead of `chunk_id`.
- `memory_stats` is removed; `memory_list` ends with per-kind counts and a total.

`knowledge_ingest` now takes `collection` instead of `domain`. Memory search/list filters
use `kind` and `delivery` instead of `memory_kind` and `delivery_policy`.

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
   ([ADR 0108 D9](/adr/0108-context-architecture-v2)): `newest` (default)
   takes the newest `memory.max_sessions` summaries (10, and `memory.max_tokens`
   caps the block — both are ceilings, and both were inert until #3308);
   `relevant` takes only sessions whose content
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
   lazy FTS5 index and returns ids to expand. That search is **stemmed but not
   semantic** — it finds "audit logs" for "audit log", and nothing at all for a
   query that shares no word stem with the stored text ("retention" vs
   "retained"). The digest's job is to remove that guess: it says which sessions
   exist so the agent knows there is something to reach for.
2. **Always-on memory ("hot").** Memory rows with `delivery="always"` inject
   every turn, loaded fresh so a just-added fact is seen immediately. The reader
   caps the set at 100 rows and 6,000 characters. Rejected, expired, invalidated
   and superseded memories never deliver. Always-on is a delivery choice, not a
   domain. With `knowledge.hot_write_confirm` enabled, `memory_save` refuses
   `delivery="always"`, reserving promotion for operator surfaces. The ingestion
   tool also retains a compatibility refusal for `collection="hot"`. Always-on
   writes emit `memory.hot_written` with a handle, source, provenance and preview.
3. **Knowledge RAG hits.** The knowledge store is searched with the last user message and the
   top-k results (default 5) inject, each line ending with its stored date and
   trust label — `(stored 2026-07-01; trust: agent)`. Two policies shape the
   list: **namespace scoping** (`knowledge.inject_namespaces` restricts what may
   enter the prompt *unasked*; tool-driven `knowledge_search` stays unscoped) and
   **trust tiers** (below).

The always-on `<available_skills>` index stays **outside** the envelope — it is
capability, not memory ([ADR 0060](/adr/0060-skill-progressive-disclosure)).

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
delivery ([ADR 0108](/adr/0108-context-architecture-v2) D2). The result is
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
2. **Always-on memory** — `delivery="always"`.
3. **The skill index** — capability awareness.
4. **The prior-session digest** — cross-session continuity.
5. **RAG hits** — relevance-matched knowledge.

Over budget, the lowest-priority parts shed first, each step re-measured so
nothing is cut mid-line: RAG hits go one whole hit at a time from the
lowest-ranked end, then the prior-session digest one **entry** at a time from
the end (oldest under `newest`, lowest-rank under `relevant` — ADR 0108 D9;
the section drops when none remain), then the skill index gives up
descriptions one row at a time down to its identity floor — every skill's name
stays listed ([ADR 0060](/adr/0060-skill-progressive-disclosure), #2867).
Working state and always-on memory are **never shed**: if they alone exceed
the budget they are delivered anyway and a warning names the sizes (once per
distinct standing-context size), because a silently missing standing
instruction is worse than an oversized prompt. The order is fixed by the ADR —
there is no `delivery_order` knob to reorder it.

What was shed is visible: the prompt **preview API** (`GET /api/prompts/preview`)
carries the `budget` summary — ceiling, chars used, and an `overflow` list
(label, items and chars dropped per part) — and marks each shed section
`truncated`. The injection
log records the ids that actually **entered** the turn, never those merely
retrieved. With the default 8% on a 128k-window model the ceiling is ~10k
tokens — more than a typical turn injects (6 000 chars of always-on memory, a
~2 000-token digest, ten hits, a 2%-of-window skill index) — so nothing is
shed there until you lower it; on a 32k window or smaller the 16k-char floor is
the budget, so RAG hits and skill descriptions beyond it now shed where they
used to be unbounded.

### Trust tiers

Knowledge chunks' `source_type` ranks into three deterministic tiers
(`knowledge/trust.py` — a code-level map, not config):

| Tier | Label | Who wrote it |
|---|---|---|
| 3 | `operator` | the operator, deliberately, through a console surface |
| 2 | `agent` | agent-derived sources retained in the knowledge backend; history content is excluded from automatic retrieval |
| 1 | `external` | ingested third-party content (web, YouTube, PDF, media) — **and any unknown/unstamped source** |

Auto-injected RAG hits are stable-sorted by tier after retrieval — an external
hit never outranks an operator- or agent-authored one, while relevance order is
preserved within a tier. A floor (`knowledge.inject_min_trust`) can exclude low
tiers from auto-injection entirely; excluded content stays reachable on demand
via `knowledge_search`, tier visible. The knobs, with worked examples:
[Knowledge reference → Delivery controls](/reference/knowledge#memory-delivery-controls-adr-0069).

### Incognito threads

Incognito suppresses automatic memory delivery and persistence for the current
turn: no session summary, digest, hot memory, or document-hit injection. Harvest
skips incognito threads and compaction writes no archive; the skill index still
injects because it describes capabilities. The chat transcript still persists.
Explicit memory tools, plugin operations, and delegate retention are independent
of this flag.

The console sends the flag on every message while the tab is incognito.
`POST /api/chat` accepts `"incognito": true`; A2A messages carry it in metadata.
API callers must set it on every turn. A thread's latest operator message
controls its mode, so turning incognito off can let later summarization include
earlier content. Start a new ordinary chat to keep the histories separate.
Manual `/compact` refuses in incognito because it requires an archive; automatic
compaction can still shorten the live context without archiving it. See
[Use incognito](/guides/manage-memory#use-incognito) for the app controls.

## Forensics: the injection log

Every model call that had memory injected appends one row to an
instance-scoped SQLite log (`<instance_root>/memory-injections.db`): which
digest sessions, which always-on memory handles, which RAG chunk ids entered the prompt,
for which session, at what approximate token cost. Served at
`GET /api/memory/injections`. This closes the audit chain — store row → source
session → the exact turns it was injected into — which is what turns
SpAIware-class memory poisoning from undetectable into greppable.

The console's **Memory** view is the inspect-audit-prune surface built on it,
through these views:

- **Sessions** — the summary files behind the digest (rows reuse the exact
  digest derivation, so what you see is what the agent is told); view/delete,
  jump to a session's injections.
- **Hot memory** — durable memories and backend hot chunks; view/edit/delete/review.
- **History** — session archives, summaries and reports.
- **Injections** — the per-turn record, filterable by session.

Document collections are browsable under **Knowledge → Store**; they are separate from durable memories.

## Semantic recall (embeddings)

The knowledge store is keyword-only FTS5 **by default** (`knowledge.embeddings: false`):
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
`embed_model` must name a model your [LiteLLM gateway](/explanation/litellm-gateway)
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

[Knowledge settings and tools](/reference/knowledge) owns retrieval and delivery
settings. [Configuration](/reference/configuration#context) owns context budgets;
[environment variables](/reference/environment-variables#memory) cover path and
persistence overrides. Use [Tune recall](/guides/knowledge) to compare changes.

## See also

- [ADR 0021 — Agent memory: extract, don't dump](/adr/0021-agent-memory-architecture)
- [ADR 0069 — Memory delivery layer](/adr/0069-memory-delivery-layer) — digest, framing, provenance, trust tiers, injection record
- [ADR 0070 — Background results](/adr/0070-background-results-push-resume) — why `background:*` workers leave no summary trail
- [Manage memory](/guides/manage-memory) — inspect, correct, reject, and delete saved context
- [Tune knowledge recall](/guides/knowledge) — check retrieval and compare settings
- [Knowledge reference](/reference/knowledge) — settings, tools, and delivery controls
- [ADR 0041 — Workspaces & tiered stores](/adr/0041-workspaces-and-tiered-stores) — the private/commons tiering behind `knowledge.scope`
- [Run a fleet](/guides/fleet) — sharing a knowledge commons across many agents on one host
- [Prompt contracts](/explanation/prompt-contracts) — the stable prompt this projected context is deliberately kept out of, and the size ceilings that keep it cacheable
- [Model output](/explanation/output-protocol) — native reasoning + the leaked-reasoning guard this enforces
- [Skills](/guides/skills) — procedural memory (Playbooks)
- [Starter tools](/reference/starter-tools) — the `memory_*` tools
