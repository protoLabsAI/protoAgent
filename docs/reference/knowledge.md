# Knowledge settings and tools

Configure retrieval in **Settings → Knowledge** or the `knowledge:` block of
`langgraph-config.yaml`. Use [Tune knowledge recall](/guides/knowledge) for a
comparison procedure and [Manage memory](/guides/manage-memory) for curation.

## Store selection

Keyword-only FTS5 is the default (`embeddings: false`). `embeddings: true`
enables keyword/vector search with reciprocal rank fusion (RRF), provided the
gateway serves `embed_model`. Repeated embedding failures open a circuit breaker
and leave keyword search available. Plugins can supply `knowledge.backend` or
`knowledge.embedder`; see [ADR 0031](/adr/0031-pluggable-knowledge-backend).

## Retrieval settings {#the-knobs}

| Key under `knowledge` | Default | Effect |
| --- | --- | --- |
| `embeddings` | `false` | Enable hybrid search |
| `embed_model` | `qwen3-embedding` | Embedding model served by the gateway |
| `top_k` | `5` | Maximum document hits automatically injected per turn; `0` stops this injection |
| `vector_k` | `20` | Vector candidates before rank fusion |
| `rrf_k` | `60` | Rank-fusion smoothing constant; this is not a semantic-versus-keyword weight |
| `min_score` | `0.0` | Retrieval score floor; `0` keeps all scored matches |
| `recall_preview_chars` | `1000` | Maximum preview length per injected hit |
| `embed_breaker_threshold` | `2` | Consecutive failures before the embedding circuit opens |
| `embed_breaker_cooldown_s` | `300` | Seconds before retrying the embedding route |
| `facts` | `true` | Extract facts when conversations are harvested |
| `db_path` | `/sandbox/knowledge/agent.db` | Legacy default resolves to `<instance_root>/knowledge/agent.db` |

Chunk sizes and contextual enrichment affect ingestion, not existing chunks;
see [Ingestion](/guides/ingestion). Tune rank fusion with retrieval evaluations
rather than assuming a lower `rrf_k` favors one search engine.

### Context and session digest

These keys live outside the knowledge block:

```yaml
context:
  budget_pct: 8
  prior_sessions: newest  # newest | relevant | off
memory:
  max_sessions: 10
  max_tokens: 2000
```

`budget_pct` limits injected context to a percentage of the model window,
with a 16,000-character floor. `0` makes it unbounded. Without a known model
window the percentage is inert and delivery is unbounded, with a log warning.
Over budget, document hits give way first, then session-digest entries, then
skill descriptions. Working state and always-on memory are retained.

`prior_sessions: newest` selects recent summaries. `relevant` uses session-search
FTS and falls back to newest; `off` disables the automatic digest while keeping
on-demand session tools. The active chat's summary is always excluded. Quoted
and bare YAML `off` both work.

`max_sessions` and `max_tokens` are digest ceilings, not switches. Non-positive
values restore defaults with a warning. Token cost is estimated as characters
/ 4. Summaries use the instance store, or `MEMORY_PATH`; there is no `memory.path`.
`GET /api/prompts/preview` reports the delivery budget, usage, and omitted parts.

## Agent memory tools {#the-agent-s-memory-tools}

| Tool | Contract |
| --- | --- |
| `memory_ingest(content, domain, heading?, memory_kind?, subject?, delivery_policy?, expires_in_days?)` | Save a fact or note. Agent writes start pending. Delivery policy is `always`, `retrieved` (default), or `on_demand` |
| `knowledge_ingest(source, domain, title?)` | Fetch and ingest a URL or local file through the document/media pipeline |
| `memory_recall(query, k=5, domain?, memory_kind?, delivery_policy?, include_superseded=False)` | Search memory; optional typed filters narrow results. Superseded rows appear only when requested, tagged accordingly |
| `session_search(query, limit=5, surface?)` | Search prior session transcripts |
| `recall_session(session_id)` | Read a saved session summary |
| `memory_list(domain?, limit=10, memory_kind?, delivery_policy?, review_state?)` | Browse recent chunks, including typed-memory metadata |
| `memory_stats()` | Count chunks per domain |
| `forget_memory(chunk_id, reason?)` | Permanently delete one chunk |

The console uses `GET /api/knowledge/search`; `GET /api/runtime/status` reports
store status. Keyword recall matches whole words. The search endpoint's opt-in
`prefix=1` flag expands the last token to a prefix, used by the palette. It does
not change ordinary recall, automatic injection, or Knowledge Store search.

## Automatic delivery controls {#memory-delivery-controls-adr-0069}

### Namespace scope {#scope-the-auto-inject-to-namespaces}

```yaml
knowledge:
  inject_namespaces: []  # no filter: all namespaces eligible
  # inject_namespaces: ["projects/alpha", ""]
```

A nonempty list restricts automatic document injection; `""` selects
unnamespaced chunks. Session attachments use `attach:<session_id>`. Both keyword
and vector rankings are filtered. Tool-driven `memory_recall` remains unscoped.

### Trust floor {#trust-tiers-adr-0069-d8}

`knowledge.inject_min_trust` defaults to `1`: external/unknown sources. Set it
to `2` to admit agent-derived and operator-authored content, or `3` for operator
content only. Automatic hits sort by trust tier after retrieval, preserving
relevance within each tier. The floor affects automatic injection, not
`memory_recall`; citations expose the source's trust tier.

### Always-on writes {#hot-memory-write-visibility-adr-0069-d8}

`knowledge.hot_write_confirm: true` refuses the agent's always-on writes through
`memory_ingest` and hot-domain `knowledge_ingest`, instructing it to ask the
operator. Nothing is queued for approval or partly stored. The default is
`false`. Console writes are unaffected; this is not a universal plugin-write gate.

Always-on is a delivery policy. A `hot` domain write receives it automatically,
and another domain can request `delivery_policy="always"`. These writes emit
`memory.hot_written` with chunk ID, source, source type, and preview.

For incognito request fields and retention boundaries, see
[Memory delivery](/explanation/memory-and-knowledge#incognito-threads).

## Review and removal {#staleness-supersede-dont-delete}

Operator writes start confirmed; agent and external writes start pending.
Pending content remains eligible for delivery. A rejected row stays stored but
is excluded from delivery. `POST /api/memory/chunks/{id}/review` accepts
`{"state": "confirmed" | "rejected" | "pending"}`. Editing preserves lifecycle
metadata and confirms the new revision, except a rejected row stays rejected.
Commons rows must use curated sharing/removal rather than this private-tier
verdict route; explicitly sending `"tier": "commons"` is refused.

Automatically revised facts keep history: the new row is inserted before the
old one is invalidated with `superseded_by:<new id>`. Default retrieval excludes
invalidated rows. `memory_recall(include_superseded=True)` or store APIs with
`include_invalidated=True` expose the history.

Single-entry operator deletes are permanent. Bulk source deletion uses
`invalidate_by_source`; the Undo action calls `restore_by_source` during the
grace window. Later bulk deletes opportunistically purge expired bulk-delete
rows. That sweep does not remove automatic supersession history.

Chat deletion's `forget=true` option removes archives in `chat-archive:<session>`
and harvested/extracted rows whose source identifies the chat or goal-iteration
subthreads. It does not reach explicit notes, hot memory, background reports,
or legacy rows without that provenance. The app defaults to harvesting ordinary
chats, excludes incognito chats, and makes forget and harvest mutually exclusive.
The API still accepts both flags: forget runs first, so the new harvest is retained. See
[the user procedure](/guides/manage-memory#delete-a-chat).

## Plugin knowledge lifecycle {#plugin-knowledge-lifecycle}

- `sdk.knowledge_purge(domain, *, before=None) -> int` hard-deletes private
  chunks in a domain, optionally before an ISO timestamp, across row, keyword,
  and vector indexes. Empty domains and invalid dates refuse with count `0`.
  A layered store does not purge its commons. Older custom backends without
  this operation return a no-op count of `0`.
- `sdk.knowledge_add(..., epoch=...)` tags an era; `knowledge_search(..., epoch=...)`
  selects that exact era in both rankings and layered tiers. Untagged and other
  eras are excluded. Omitting the filter searches every era. Store APIs expose
  equivalent `purge_domain` and `epoch` arguments.

## Fleet commons {#sharing-knowledge-across-a-fleet-the-commons}

Knowledge defaults to `scope: scoped`: private to the agent. To read shared
knowledge while keeping new writes private:

```yaml
knowledge:
  scope: layered
commons:
  path: ~/.protoagent/commons
```

`scope: shared` writes directly to the commons. `scope: layered` searches both
tiers, deduplicates matches, and writes private. **Share** in **Knowledge → Store**
promotes a private chunk (`POST /api/knowledge/{id}/promote`); **Unshare** removes
its commons copy (`POST /api/knowledge/{id}/forget`). Private copies remain.

All agents pointing at the same `commons.path` can read it, regardless of
instance ID. Give isolated fleets different commons paths. Agents sharing a
commons should use the same `embed_model`: a mismatched agent searches that
commons with keywords only and logs a warning.
