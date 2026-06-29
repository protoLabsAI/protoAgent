# Memory & the knowledge store

protoAgent has a single durable **knowledge store** and a set of conventions for
*what* goes in it and *how* it comes back out. This page explains the whole
pipeline — the store, the three kinds of memory, the write paths, the retrieval,
and the configuration — so you can reason about (and tune) what your agent
remembers.

The design rules behind it are [ADR 0021](../adr/0021-agent-memory-architecture.md)
("extract, don't dump").

## The store

`knowledge/store.py` is a SQLite database with **FTS5 full-text search** (with a
`LIKE` fallback when FTS5 isn't compiled in). One `chunks` table holds everything
the agent knows; rows are distinguished by a few columns:

| Column | Meaning |
|---|---|
| `domain` | the bucket — `fact`, `conversation`, `hot`, `finding`, or anything a tool sets (`preferences`, `context`, …) |
| `finding_type` | sub-type within a domain (e.g. `fact`, `conversation`) |
| `namespace` | optional per-project / per-owner scope (ADR 0021) — a *filter* for multi-project forks, never required |
| `source` / `source_type` | provenance (`harvest`, `tool:<name>`, …) |
| `heading`, `content`, `created_at` | the chunk itself |

## Three kinds of memory

protoAgent follows the standard semantic / episodic / procedural split, mapped
onto primitives it already has:

- **Semantic** — discrete, durable **facts** (`domain="fact"`). "The user deploys
  on Tuesdays." Extracted by the harvest pass; queryable like any chunk.
- **Episodic** — **conversation summaries** (`domain="conversation"`). A retired
  thread is summarized into one searchable chunk.
- **Procedural** — **Playbooks / skills** (`skills.db`, a separate FTS5 index).
  Methodology the agent retrieves but never "runs". See [Skills](../guides/skills.md).

## Write paths

Everything that writes to the store funnels through `KnowledgeStore.add_chunk`:

1. **Memory tools** — the agent calls `memory_ingest` (and friends:
   `memory_recall`, `memory_list`, `memory_stats`) to record a fact the user
   shared. See [Starter tools](../reference/starter-tools.md).
2. **Harvest on retirement** — when a chat thread is retired (aged out by the
   checkpoint pruner, or deleted), `graph/conversation_harvest.py` runs a single
   **session-end pass** (cheap aux model): it stores an episodic *summary* and,
   when `knowledge.facts` is on, **extracts durable facts** and consolidates them
   (near-duplicates are skipped). This is *extract, don't dump* — it never stores
   raw turns.

### The reasoning guardrail

The agent reasons natively — on the gateway's `reasoning_content` channel, not in
the answer text (see [model output](output-protocol.md)). As a defense-in-depth
guardrail, `add_chunk` **strips any leaked `<scratch_pad>`/`<think>` from every
write** — so reasoning a provider leaks into content can never reach the store (and
never gets recycled into a later prompt via retrieval). A chunk that is *only*
reasoning is dropped, not stored empty.

## Retrieval

The `KnowledgeMiddleware` runs before each LLM turn and injects relevant context:

- **Relevance** — searches the store with the user's message and injects the
  top-k matches.
- **Hot memory** — always-on `domain="hot"` facts, injected every turn.
- **Available skills** — the always-on `<available_skills>` index (name + summary of each skill; the agent loads a full procedure on demand via `load_skill`, ADR 0060).
- **Prior sessions** — recent session summaries for cross-session recency.

The operator can browse and search the whole store in the console under
**Knowledge → Store**.

## Semantic recall (embeddings)

The store ships **hybrid by default** (`HybridKnowledgeStore`): it fuses FTS5
keyword search with **vector similarity** using Reciprocal Rank Fusion, so lexical
*and* semantic hits reinforce each other. Keyword-only search misses paraphrases —
*"how do I ship a build?"* won't match a stored *"the release pipeline is manual
via workflow_dispatch"* — which is why embeddings are on by default. An embedding
circuit breaker falls back to FTS5 on an embedding outage — quality degrades,
availability never does. Set `embeddings: false` for keyword-only.

```yaml
knowledge:
  embeddings: true             # on by default
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

All under the `knowledge:` block (see [Configuration](../reference/configuration.md)):

| Key | Default | Effect |
|---|---|---|
| `db_path` | `/sandbox/knowledge/agent.db` | store location (instance-scoped) |
| `embeddings` | `true` | hybrid semantic + keyword search (vs keyword-only) |
| `embed_model` | `qwen3-embedding` | gateway embedding model (set per your gateway) |
| `facts` | `true` | extract semantic facts during the harvest pass |
| `top_k` | `5` | how many chunks retrieval injects per turn |
| `middleware.knowledge` | `true` | turn the whole subsystem on/off |

Tip: enabling embeddings is measurable — add a recall eval and compare keyword vs
hybrid via `evals.sweep`. See [Eval your fork](../guides/evals.md).

## See also

- [ADR 0021 — Agent memory: extract, don't dump](../adr/0021-agent-memory-architecture.md)
- [Model output](output-protocol.md) — native reasoning + the leaked-reasoning guard this enforces
- [Skills](../guides/skills.md) — procedural memory (Playbooks)
- [Starter tools](../reference/starter-tools.md) — the `memory_*` tools
