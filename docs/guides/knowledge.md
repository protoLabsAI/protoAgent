# Tune the knowledge store (RAG)

The knowledge store is what the agent recalls from. By default it's **hybrid** —
keyword (SQLite FTS5) **and** semantic (vector) search, fused with Reciprocal Rank
Fusion (RRF). `KnowledgeMiddleware` injects the top hits into the system prompt every
turn. This guide is the tuning surface; for the design and write paths see
[Memory & the knowledge store](/explanation/memory-and-knowledge), and to load
content see [Ingest documents & media](/guides/ingestion).

## Which store you get

- **Default:** `HybridKnowledgeStore` (FTS5 + vectors) when `knowledge.embeddings`
  is on (the default) and `embed_model` resolves on your gateway.
- **Keyword-only:** set `embeddings: false` → FTS5 only (no embedding calls).
- **Pluggable:** a plugin can register an alternate backend (`knowledge.backend`) or
  embedder (`knowledge.embedder`) — see [ADR 0031](/adr/0031-pluggable-knowledge-backend).

If embeddings fail repeatedly the store trips a **circuit breaker** and silently
degrades to keyword-only — it's never KB-less. Test/refresh the gateway key with
**Settings → Test connection**, which clears the breaker immediately.

## The knobs

All under `knowledge:` in `langgraph-config.yaml`:

```yaml
knowledge:
  embeddings: true            # hybrid (semantic+keyword); false → keyword-only
  embed_model: qwen3-embedding # gateway EMBEDDING model (not the chat model);
                              #   must be served by your gateway (check GET /v1/models)
  top_k: 10                   # hits injected into the prompt per turn
  vector_k: 20                # vector candidates fetched before RRF fusion (hybrid)
  rrf_k: 60                   # RRF constant — higher = keyword & semantic weigh more evenly
  min_score: 0.0              # drop fused hits below this score (0 = keep all)
  recall_preview_chars: 1000  # chars of each hit the model sees in the injected block
  embed_breaker_threshold: 2  # consecutive embed failures before the breaker opens
  embed_breaker_cooldown_s: 300 # seconds the breaker stays open before retrying
  facts: true                 # harvest semantic facts from retiring conversations
  db_path: /sandbox/knowledge/agent.db  # → ~/.protoagent/knowledge/agent.db fallback
```

Rules of thumb:
- **Recall too thin?** raise `top_k` (more injected) and/or `vector_k` (bigger candidate
  pool). **Context too noisy / off-topic?** raise `min_score` to set a relevance floor.
- `rrf_k` rebalances semantic vs keyword — lower lets semantic dominate. Tune it against
  the retrieval eval harness rather than by feel.
- Chunking (`chunk_*`) and `contextual_enrichment` are ingest-time knobs — see
  [Ingest documents & media](/guides/ingestion).

## The agent's memory tools

When a knowledge store is wired, the agent gets these (operator-curatable under
**Knowledge → Store**):

| Tool | What it does |
|---|---|
| `memory_ingest(content, domain, heading?)` | store a self-contained fact/note for later recall |
| `knowledge_ingest(source, domain, title?)` | fetch + extract + store a **URL or local file** — YouTube/web/PDF/audio/video — through the full [ingestion pipeline](/guides/ingestion#from-the-agent) |
| `memory_recall(query, k=5)` | search long-term memory (hybrid, or FTS5 if the breaker is open) |
| `memory_list(domain?, limit=10)` | browse recent chunks (used by `/dream` consolidation) |
| `memory_stats()` | per-domain chunk counts |
| `forget_memory(chunk_id, reason?)` | delete one chunk by id (targeted) |

`GET /api/runtime/status` reports the store status; `GET /api/knowledge/search`
backs the console browser.

## Sharing knowledge across a fleet (the commons)

By default each agent's knowledge store is **private** (`scope: scoped`) — what one
agent learns, harvests, or ingests stays with it. To let a fleet pool knowledge, opt
a store into a shared **commons** ([ADR 0041](/adr/0041-workspaces-and-tiered-stores)),
the same tiering model as [skills](/guides/skills#sharing-skills-across-a-fleet-the-commons):

```yaml
knowledge:
  scope: layered          # read commons ∪ private, write private, promote to share
commons:
  path: ~/.protoagent/commons   # host-level, shared by every agent that points here
```

- **`scope: shared`** — the whole store *is* the commons (every write lands in it).
- **`scope: layered`** — "shared brain, private hands": the agent reads
  `commons ∪ private` (a second-level RRF fuses the two tiers, deduped by content) and
  writes to its **private** tier, so in-progress facts never pollute the fleet. You lift
  a proven chunk into the commons explicitly.

The commons is **host-level and un-scoped** — every agent pointing at the same
`commons.path` reads it, regardless of `instance.id`. Run two *isolated* fleets on one
host by giving each a distinct `commons.path`. The boot log names the active tier and
path (`[knowledge] tier=layered (… ∪ …)`).

Promotion is operator-curated from the **console**, not a CLI: in **Knowledge → Store**,
layered-mode chunks carry a `private`/`commons` tier badge with **Share** (lift a private
chunk into the commons — every agent on the box can then recall it) and **Unshare**
(remove it from the commons; the private copy, if any, is untouched). The same gestures
are `POST /api/knowledge/{id}/promote` and `POST /api/knowledge/{id}/forget`. Nothing is
auto-shared — the commons is trusted because promotion is explicit.

::: warning One embed model per shared fleet
A `shared`/`layered` fleet must agree on one `embed_model`. The commons is stamped with
the model that first wrote it; an agent that joins with a different `embed_model` serves
the commons tier **FTS5-only** (no incompatible-vector fusion) and logs a warning. Keep
`embed_model` identical across every agent that shares a `commons.path`.
:::
