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

1. **Memory tools** — the agent calls `memory_ingest` (and friends:
   `memory_recall`, `memory_list`, `memory_stats`, `forget_memory`) to record a
   fact the user shared. See [Starter tools](../reference/starter-tools.md).
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

## Delivery: what enters the prompt (ADR 0069)

`KnowledgeMiddleware` runs before each model call and assembles the injected
context. Everything memory-derived rides inside **one `<injected_memory>`
envelope** whose header states it is *reference data* — possibly stale, possibly
third-party — never instructions and never part of the current conversation
(the OWASP ASI06 memory-poisoning posture: reduce memory's authority at the
prompt layer, don't just hope the store stays clean). Three parts, in order:

1. **The prior-sessions digest.** One **attributed line per session** — id ·
   timestamp · surface (chat/a2a/…) · topic · message count — for the
   newest 10 session summaries under a ~2 000-token cap, behind a framing
   header that says these are *other, separate* sessions. The topic derives
   from the first *user* message only (no assistant text — that's the identity
   confusion + poisoning surface). The digest is cached with a 60 s TTL and
   suppressed on goal-driven turns. The full summary of any listed session is
   one tool call away: `recall_session(session_id)`.
2. **Hot memory.** `domain="hot"` chunks are always-on operator facts: the
   newest 100 under a 6 000-char budget inject **every turn**, loaded fresh per
   turn so a just-added fact is seen immediately. Because that makes a silent
   hot write the highest-leverage poisoning move available, every hot write —
   agent tool, console route, or plugin — emits a `memory.hot_written` bus
   event, and an optional gate (`knowledge.hot_write_confirm`) makes the
   agent's own write path refuse `domain="hot"` entirely, reserving always-on
   promotion for operator surfaces.
3. **RAG hits.** The store is searched with the last user message and the
   top-k results (default 10) inject, each line ending with its stored date and
   trust label — `(stored 2026-07-01; trust: agent)`. Two policies shape the
   list: **namespace scoping** (`knowledge.inject_namespaces` restricts what may
   enter the prompt *unasked*; tool-driven `memory_recall` stays unscoped) and
   **trust tiers** (below).

The always-on `<available_skills>` index stays **outside** the envelope — it is
capability, not memory ([ADR 0060](../adr/0060-skill-progressive-disclosure.md)).

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
| `hot_write_confirm` | `false` | when on, the agent's `memory_ingest` refuses `domain="hot"` writes |
| `scope` | `scoped` | tier ([ADR 0041](../adr/0041-workspaces-and-tiered-stores.md)): `scoped` (private) · `shared` (host commons) · `layered` (read commons ∪ private, write private). See [Tune the knowledge store → Sharing across a fleet](../guides/knowledge.md#sharing-knowledge-across-a-fleet-the-commons) |
| `middleware.knowledge` | `true` | turn the whole subsystem on/off |

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
- [Model output](output-protocol.md) — native reasoning + the leaked-reasoning guard this enforces
- [Skills](../guides/skills.md) — procedural memory (Playbooks)
- [Starter tools](../reference/starter-tools.md) — the `memory_*` tools
