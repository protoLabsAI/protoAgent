# ADR 0125 — Separate memory, knowledge and history

- Status: Proposed
- Date: 2026-10-28
- Deciders: Josh Mabry; protoAgent maintainers
- Implementation: Phase 0 in #4553; Phases 1–3 not started.
- Builds on: [ADR 0021](./0021-agent-memory-architecture.md) (extract, don't dump),
  [ADR 0031](./0031-pluggable-knowledge-backend.md) (knowledge backend seam),
  [ADR 0041](./0041-workspaces-and-tiered-stores.md) (commons tier),
  [ADR 0069](./0069-memory-delivery-layer.md) (delivery layer, trust tiers),
  [ADR 0108](./0108-context-architecture-v2.md) (D4 orthogonal memory attributes).
- Supersedes: ADR 0108 D4's "one table, orthogonal columns" model for memory rows.

## Context

Every agent has one knowledge store: a SQLite `chunks` table (FTS5 plus vectors) that
holds three different things:

- **Memory**: what the agent has learned about its operator and its work, such as
  preferences, decisions, standing rules and facts. It is small, should be curated, and
  is worth reviewing.
- **Knowledge**: reference material the operator gave it, such as books, PDFs, URLs and
  YouTube transcripts. It is large, read-only after ingest, and organised by collection.
- **History**: what happened. This covers compaction archives (#2784), retired-thread
  summaries (`conversation_harvest`) and background job reports (ADR 0070 D2). It is
  written automatically, grows without bound, and the checkpointer and trajectory
  already hold the source.

They share one search path. ADR 0069 and 0108 handled the overlap by adding columns
(`memory_kind`, `source_type`, `delivery_policy`, `namespace`, `review_state`,
`epoch`, `expires_at`, `subject`, `tier`, `invalidated_at`, …) on top of `domain`, which
still mixes "what kind of row" with "which bucket". Every reader and writer has to get
about 12 fields right, and the default search covers all three layers.

### Evidence (live fleet, 2026-10-28)

What the stores hold:

| Agent | Rows | History | Memory | Knowledge |
|---|---|---|---|---|
| managingEditor | 3,048 | 3,041 | 7 | 0 |
| protoEngineer | 1,505 | 1,218 | ~80 | 153 (`claude-import`) |
| jobCoach | 1,025 | 887 | ~70 | 38 |
| necroMunda | 1,218 | 5 | 2 | 1,211 (rulebooks) |

What automatic retrieval injected into prompts over the previous 7 days (injection log,
ADR 0069 D6):

| Agent | Turns | Share of RAG hits that were history |
|---|---|---|
| managingEditor | 758 | 99% |
| jobCoach | 56 | 73% |
| protoEngineer | 210 | 59% (plus 34% `claude-import`) |

managingEditor writes about 170 archive rows a day from its scheduled-wake thread. Each
turn's top-k retrieval then returns copies of its own wake prompt.

How agents use the memory tools (live checkpoints, ~350 threads across 10 agents): about
60 `memory_ingest`, 54 `memory_recall` and 45 `recall_session` calls in total. Most of
what they save is work status, such as "v0.218.1 cut end to end by me", "#4318 merged at
head cac39166" and "bd-8cfo is being split". That status belongs on the board or the
issue, and it goes stale within days.

Stale facts are never reconciled. jobCoach has two live rows on the same subject: an
extracted fact from 10-15, "Josh's minimum acceptable base compensation is $185K", and an
always-on note from 10-16, "Comp floor $120K base (was $185K)". The extractor and the
agent's own writes never meet, and nothing supersedes the older row.

## Decision

### D1 — Three stores, not one table

| Layer | Holds | Written by | Store |
|---|---|---|---|
| **Memory** | Preferences, decisions, standing rules, facts about the operator, projects and environment | The agent, deliberately; the extractor proposes; the operator reviews | `memories` table, core SQLite (FTS5 plus vectors) |
| **Knowledge** | Ingested documents in named collections; private plus commons tiers (ADR 0041) | `knowledge_ingest` and operator uploads only | `chunks`; still the ADR 0031 `KnowledgeBackend` seam |
| **History** | Compaction archives, thread summaries, job reports | The runtime, automatically | `history` table, core SQLite (FTS5 only, no embeddings) |
| Skills | How to do things | Unchanged (ADR 0060) | Unchanged |

Each store has its own schema with only the fields it needs:

- **Memory:** `kind` (profile / standing / fact / decision), `subject`, `content`,
  `delivery` (always / retrieved / on_demand), `review_state`, `provenance` (operator /
  agent / extracted), `scope` (namespace), `valid_from`, `superseded_by`,
  `expires_at`, `source_session`. There is no `domain`.
- **Knowledge:** `collection` (replaces `domain`), `source` (URL or path), `title`,
  `media_type`, `tier`, `ingested_at`, chunk position.
- **History:** `session_id`, `surface` (chat / background / a2a / scheduled), `kind`
  (archive / summary / report), `span_start`, `span_end`, `content`.

Episodic memory (ADR 0021's "what happened") is history. Agents reach it with
`session_search` and `recall_session`; it is not memory.

### D2 — Only memory and knowledge reach the prompt unasked

The projected context (ADR 0108 D1) has these parts:

- **Always-on memory**: `delivery=always` rows, capped as hot memory is today.
- **Retrieved memory**: top-k from `memories`.
- **Retrieved knowledge**: top-k from `chunks`, cited by collection and source.
- **The prior-sessions digest**: an index of history (session ID, date, topic; ADR 0069
  D1), never its content.

Memory and knowledge get separate budgets and separate labelled sections. A 1,200-chunk
rulebook can no longer push out the agent's three standing rules, and the reverse holds
too. History content never auto-injects. Phase 0 (#4553) already enforces this on the
current single table.

### D3 — One tool family per layer

| Layer | Tools |
|---|---|
| Memory | `memory_save`, `memory_search`, `memory_list`, `memory_forget` |
| Knowledge | `knowledge_ingest`, `knowledge_search` |
| History | `session_search`, `recall_session` |

`memory_save` replaces `memory_ingest`. "Ingest" means fetching a document, which is
what `knowledge_ingest` does. `memory_recall` becomes `memory_search` and covers memory
only. The old names are removed in the same release; bundled skills and SOULs that name
them are updated in that PR. This follows the no-legacy rule for fleet floors.

### D4 — Saving a memory checks for the same subject

- `memory_save` with a `subject` returns the live memories already on that subject.
- If the new row has the same `subject` and `kind` as an existing one, it supersedes the
  older row (`superseded_by`). The older row stays for audit and is never delivered.
- The agent sees what it replaced in the tool result, so a wrong supersession is visible
  and the operator can revert it from the inspector.

This is deterministic: no LLM reconciles at write time. That keeps ADR 0069's finding
that write-time LLM reconciliation is a trap.

The retirement extractor writes through the same funnel as `provenance=extracted`,
`review_state=pending`. An extracted fact therefore supersedes, or is superseded by, the
agent's own row on the same subject, which fixes the $185K/$120K case.

### D5 — Work status is not memory

The tool description and the operating model say what is worth saving: things that will
still be true and useful in a month. Release logs, PR states and card progress live on
the board, issues and PRs, where they stay current. If a `memory_save` call looks like a
status line (a PR, card or SHA reference with a past-tense event and no subject), it
still saves but returns a nudge. It is not refused.

### D6 — Migration

A one-shot migration runs at boot and is idempotent (`_kb_meta` marker). It routes each
existing row as follows:

| Existing row | Goes to |
|---|---|
| `domain = conversation` (archives, harvest, background reports) | `history` |
| `source_type` is ingest / url / youtube / pdf / text / file, or a non-memory freeform domain (`necromunda-core`, `claude-import`, …) | `chunks`, with `collection` set to the old domain |
| Everything else (`hot`, `preferences`, `fact`, `context`, `general` written by the agent) | `memories`, with `kind` from `memory_kind` and `delivery=always` for `hot` |

The pre-migration database is kept as `agent.db.pre-0125` until the next release. The
console's Memory inspector reads `memories`, its Knowledge browser reads `chunks`, and a
History tab reads `history`.

### D7 — Out of scope

- Shared memory across agents: memory stays per agent; knowledge has the commons tier.
- Memory evals (LongMemEval subset): wanted, but tracked separately.

## Phases

0. **#4553**: history rows stop auto-injecting (shipped on the current table).
1. **History store**: add the `history` table, point compaction, harvest and job reports
   at it, migrate `domain=conversation` rows, and move `session_search` and
   `recall_session` onto it.
2. **Memory/knowledge split**: add the `memories` table, migrate, split retrieval and
   budgets (D2), rename the tools (D3), and split the console inspector.
3. **Better memory use**: subject supersession (D4), status-line guidance (D5), the
   extractor through the memory funnel, and a review pass over each agent's migrated
   memories with the operator.

## Consequences

- Retrieval sees only memory and knowledge. On today's fleet that is 1–10% of rows, so
  auto-injected context gets smaller and more relevant.
- Each layer's schema shows its intent. Nothing has to remember that `domain="hot"`
  means always-on, or that `domain="conversation"` is history.
- History is no longer embedded, which stops the embedding cost on ~170 rows a day per
  busy agent.
- A plugin `KnowledgeBackend` (ADR 0031) now backs only knowledge. Memory and history are
  core SQLite, so a vector-DB plugin can no longer hold the agent's memory. That is
  deliberate: memory is small and needs review.
- One breaking rename (D3) touches bundled skills and agent SOULs that name
  `memory_ingest` or `memory_recall`.
