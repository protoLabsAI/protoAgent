# Tune the knowledge store (RAG)

The knowledge store is what the agent recalls from: keyword (SQLite FTS5) search
out of the box, **hybrid** keyword + semantic (vector) search — fused with
Reciprocal Rank Fusion (RRF) — once you opt in. `KnowledgeMiddleware` injects the
top hits into the system prompt every turn. This guide is the tuning surface; for the design and write paths see
[Memory & the knowledge store](/explanation/memory-and-knowledge), and to load
content see [Ingest documents & media](/guides/ingestion).

## Which store you get

- **Default:** keyword-only FTS5 — `knowledge.embeddings` ships **off** (#1681:
  out of the box the app must not depend on an optional gateway route; a gateway
  without a working embedding model turned every turn's recall into a stall).
- **Hybrid:** set `embeddings: true` once your gateway serves `embed_model` →
  `HybridKnowledgeStore` (FTS5 + vectors, RRF-fused).
- **Pluggable:** a plugin can register an alternate backend (`knowledge.backend`) or
  embedder (`knowledge.embedder`) — see [ADR 0031](/adr/0031-pluggable-knowledge-backend).

If embeddings fail repeatedly the store trips a **circuit breaker** and silently
degrades to keyword-only — it's never KB-less. Test/refresh the gateway key with
**Settings → Test connection**, which clears the breaker immediately.

## The knobs

All under `knowledge:` in `langgraph-config.yaml`:

```yaml
knowledge:
  embeddings: true            # opt-in hybrid (semantic+keyword); default false → keyword-only
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
| `forget_memory(chunk_id, reason?)` | **hard-delete** one chunk by id (targeted; see [staleness](#staleness-supersede-dont-delete) — explicit deletes are real deletes) |

`GET /api/runtime/status` reports the store status; `GET /api/knowledge/search`
backs the console browser.

## Memory delivery controls (ADR 0069)

What the store *holds* and what gets *pushed into the prompt each turn* are separate
surfaces ([ADR 0069](/adr/0069-memory-delivery-layer)). These controls gate and audit
the delivery side:

### Scope the auto-inject to namespaces

Every chunk carries an optional `namespace` (session attachments use
`attach:<session_id>`; forks/plugins set their own). By default the per-turn
auto-inject searches **everything**. To restrict what can enter the prompt unasked:

```yaml
knowledge:
  inject_namespaces: []       # default: empty = no filter (everything eligible)
  # inject_namespaces:        # when set: only these namespaces auto-inject
  #   - "projects/alpha"
  #   - ""                    # the empty string matches UN-namespaced chunks
```

This gates **only the automatic injection** (`KnowledgeMiddleware`) — tool-driven
recall (`memory_recall`) is deliberately unscoped, so out-of-scope knowledge stays
reachable on demand with the model's intent visible as a tool call. Hybrid stores
filter both the keyword and vector rankings, so a fused hit can never come from
outside the scope.

### Incognito threads

A turn flagged **incognito** leaves no memory trail and reads none in: the
session-summary write is skipped (nothing to show up in later threads'
`<prior_sessions>` digest), the digest / hot-memory / RAG injection is skipped
for that turn (the skill index still injects — it's capability, not memory), and
the retire-time conversation harvest skips the thread (its transcript is never
summarized into the knowledge store).

- **`POST /api/chat`** — pass `"incognito": true` in the request body (additive;
  default `false`).
- **A2A / console streaming path** — set `incognito: true` in the message
  **metadata** (alongside `model` / `reasoning_effort`).
- **Console** — toggle a chat tab incognito with the `/incognito` slash command
  or the tab's right-click menu ("Turn incognito on/off"; "New incognito chat"
  starts a thread private — **Shift+click** the tab bar's `+` does the same).
  While ON the tab shows an eye-off glyph and the composer an `incognito` chip
  (click it to turn off), and the console stamps the metadata flag onto
  **every** message it sends from that tab.

The flag is per-message and stamped explicitly on every turn, so a thread is only
as incognito as its latest message — a raw API caller must send the flag on each
turn of a thread it wants kept out of memory (the console toggle does exactly
that for you).

### The per-turn injection record

Every model call that had memory injected appends one row to an instance-scoped
log (`<instance_root>/memory-injections.db`) recording **which** digest sessions,
hot-memory chunk ids, and RAG chunk ids entered the prompt, and roughly how many
tokens they cost. This is the forensics half of the poisoning story: store row →
source session → the turns it was injected into.

```
GET /api/memory/injections?session_id=<id>&limit=50
```

returns `{"injections": [...]}` newest-first — omit `session_id` for all sessions.
Each row: `ts`, `session_id`, `digest_session_ids`, `hot_chunk_ids`,
`rag_chunk_ids`, `approx_tokens`.

### Trust tiers (ADR 0069 D8)

Not everything in the store deserves the same seat at the table. Every chunk's
`source_type` names the write path that created it, and those paths rank into
**three deterministic trust tiers** (`knowledge/trust.py` — a code-level map,
not config):

| Tier | Label | Write paths |
|---|---|---|
| 3 | `operator` | console knowledge browser add/edit, memory-inspector hot edit (`source_type: operator`/`manual`) |
| 2 | `agent` | extracted facts (`extracted`), harvest summaries (`harvest`), `memory_ingest` + compaction archives (`conversation`), findings (`chat`) |
| 1 | `external` | everything ingested: web pages (`html`), YouTube transcripts, PDFs, transcribed media, pasted docs — **and any unknown/unstamped `source_type`** (least trust by default, incl. rows written before stamping existed) |

Two things happen with the tier:

- **Auto-injection down-weights low tiers, always.** The per-turn RAG hits are
  stable-sorted by tier after retrieval — an external/ingested hit never
  outranks an operator- or agent-authored one; relevance order is preserved
  within a tier. Deterministic and post-score, so it behaves identically on
  the plain, hybrid, and layered stores.
- **A trust floor can exclude tiers entirely:**

```yaml
knowledge:
  inject_min_trust: 1   # default: nothing excluded (down-weighting only)
  # inject_min_trust: 2 # exclude ingested/external content from auto-injection
  # inject_min_trust: 3 # auto-inject operator-authored rows only
```

The floor gates **only the automatic injection** — like `inject_namespaces`,
tool-driven recall (`memory_recall`) is never gated, so excluded content stays
reachable on demand with the model's intent visible as a tool call. The tier is
visible everywhere it travels: auto-injected lines end with
`(stored 2026-07-01; trust: external)`, and `memory_recall` / `memory_list`
citations carry the same `trust:` label.

### Hot-memory write visibility (ADR 0069 D8)

`domain="hot"` chunks are injected in front of the model **every turn**, which
makes a silent hot write the highest-leverage poisoning move there is. Two
controls:

- **Every hot write is a visible event.** Any write that creates a hot chunk —
  the agent's `memory_ingest`, the console routes, a plugin via the SDK —
  emits `memory.hot_written` on the plugin event bus ([ADR 0039](/adr/0039-plugin-event-bus))
  with `{chunk_id, source, source_type, preview}`. Consoles and plugins can
  subscribe (`HOST.on("memory.hot_written", …)`) to toast/log it.
- **An optional confirm gate** for multi-user or higher-paranoia setups:

```yaml
knowledge:
  hot_write_confirm: false  # default: agent hot writes allowed (single-operator flow)
  # hot_write_confirm: true # memory_ingest REFUSES domain="hot" writes with a clear
                            # error telling the model to ask you; only the console
                            # (Knowledge → Store / memory inspector) writes hot memory
```

The gate binds the **agent's own write path** (`memory_ingest`) — the simple
mechanism: a refusal with instructions, nothing is parked or half-stored.
Operator console surfaces stamp `source_type: operator` and are unaffected.

### Staleness: supersede, don't delete

LLMs demonstrably can't self-adjudicate freshness, so protoAgent handles
staleness **deterministically at retrieval time** ([ADR 0069](/adr/0069-memory-delivery-layer)
D9) instead of judging it with a model at write time:

- **Facts are superseded, never silently replaced.** When the session-end fact
  pass extracts a fact that *revises* one already stored (same subject, changed
  details — detected by a deterministic token-overlap band, no LLM involved),
  the old row is stamped `invalidated_at` and the new row inserted. History is
  kept for audit; nothing is updated in place or deleted.
- **Retrieval excludes invalidated rows by default.** `search`/`list_chunks`
  on all three stores (plain, hybrid — both rankings — and layered), hot-memory
  injection, and `memory_recall` only surface valid rows. Audit tooling can
  pass `include_invalidated=True` (store API) to see the full history.
- **Recency is surfaced in-context.** Each auto-injected RAG line ends with the
  chunk's stored date — `(stored 2026-07-01)` — and `memory_recall` cites dates
  per hit, so the model weighs freshness from explicit timestamps.
- **Operator deletes stay hard deletes.** `forget_memory` and the memory
  inspector's DELETE routes remove rows outright — explicit operator intent
  beats history-keeping. Supersession is only for the *automatic* write paths.

### Plugin knowledge lifecycle

Supersession covers facts that get *revised*; a long-running **plugin's** knowledge can
become wrong wholesale — spacetraders' game universe wipes weekly, so last week's route
lessons reference markets that no longer exist. The consumption SDK
([ADR 0043](/adr/0043-plugin-consumption-sdk-workflows-extraction)) gives plugins two
lifecycle tools for that (#1634):

- **Purge a domain** — `sdk.knowledge_purge(domain, *, before=None) -> int`
  hard-deletes every chunk in a domain (optionally only those created before an
  ISO-8601 timestamp) and returns the count. The delete is consistent across **every
  index** — main rows, the FTS index, and the hybrid store's vectors — so a purged
  chunk can't linger as a vector-only hit. On a layered store only the **private**
  tier is purged; the shared commons is curated (promote/forget), never bulk-deleted.
  An empty domain or an unparseable `before` refuses (returns 0) rather than risk
  deleting the wrong rows.
- **Scope by epoch** — `sdk.knowledge_add(..., epoch="2026-06-29")` tags a chunk with
  the era it was learned in (an opaque string, typically a reset date), and
  `sdk.knowledge_search(..., epoch=...)` filters **both rankings** (FTS + vector, and
  both layered tiers) to exactly that era — other epochs *and* untagged chunks don't
  match. A wipe becomes a new tag: old lessons stay stored for post-mortem analysis
  but stop polluting retrieval. Unfiltered search (`epoch=None`) still sees every era.

Both work on the store API too (`purge_domain`, the `epoch` kwarg on
`add_chunk`/`add_document`/`search`). A custom [ADR 0031](/adr/0031-pluggable-knowledge-backend)
backend that predates this surface keeps working: `knowledge_purge` degrades to a
0-count no-op, and the SDK only forwards `epoch` when a caller passes one.

### The Memory inspector (console)

The **Memory** rail view is the operator half of all of the above — a security
control first (SpAIware-class memory poisoning gets *detected* here), UX second:

- **Sessions** — the persisted summaries behind the `<prior_sessions>` digest,
  one row per session (id · surface · topic · message count). Click a row to
  read the full summary (exactly what `recall_session` returns) in the document
  viewer; delete a row to forget that session.
- **Hot memory** — the always-on `domain="hot"` chunks injected into every turn,
  with their provenance (`source` = the session that wrote them); edit or delete
  per row. Deleting stops the injection immediately.
- **Injections** — the per-turn record above, filterable by session id; a
  session row's syringe button jumps straight to its filtered record.

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
