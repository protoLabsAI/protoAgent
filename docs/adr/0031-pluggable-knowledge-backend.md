# ADR 0031 — Pluggable knowledge backend

**Status:** Accepted

## Context

The knowledge base is **model-pluggable but not backend-pluggable**. Today:

- `knowledge/store.py::KnowledgeStore` — SQLite + FTS5 keyword search (the default).
- `knowledge/hybrid_store.py::HybridKnowledgeStore(KnowledgeStore)` — adds a vector
  column + RRF-fused semantic search (ADR 0021), embeddings via `create_embed_fn`.
- `create_embed_fn` routes `embed_model` through the OpenAI-compatible **gateway**, so the
  embedding *model* is swappable by config (`embed_model` + `api_base`) — OpenAI,
  `nomic-embed-text` via Ollama, a vLLM-hosted model, etc.
- `server/agent_init.py::_build_knowledge_store` picks Hybrid vs FTS5, degrade-safe.

What you **can't** do without editing core: swap the *store backend* — pgvector, Qdrant,
Chroma, Weaviate, a managed vector DB. The interface to do so already exists implicitly
(`HybridKnowledgeStore` subclasses `KnowledgeStore`; every consumer — memory tools,
`KnowledgeMiddleware`, the knowledge routes, the eval harness — uses `STATE.knowledge_store`
**duck-typed**), but there's no **seam**: no Protocol, no factory override, and no plugin
hook (we have `register_*` for tools/skills/verifiers/views/MCP/goal-hooks, but nothing for
the store). A fork wanting pgvector must subclass + edit `_build_knowledge_store`, which
violates the operator-fork contract (forks ADD, don't EDIT core).

## Decision

Make the knowledge backend pluggable the same way everything else is — a documented
interface + a plugin hook + a config selector — with the SQLite store as the default
reference. Three pieces:

### D1 — a `KnowledgeBackend` Protocol

Formalize the duck-typed contract consumers already rely on (`knowledge/backend.py`):

```python
@runtime_checkable
class KnowledgeBackend(Protocol):
    def add_chunk(self, content: str, domain: str = "general", **kw) -> int | None: ...
    def search(self, query: str, k: int = 5, *, domain: str | None = None) -> list[dict]: ...
    def get_hot_memory(self, max_chars: int = 6000) -> str: ...
    def list_chunks(self, *a, **kw) -> list[dict]: ...
    def stats(self) -> dict: ...
    def delete_by_id(self, chunk_id: int) -> bool: ...
    def add_finding(self, *a, **kw): ...
```

The built-in `KnowledgeStore` already satisfies it. It's documentation + an optional check;
duck-typing still works (a backend may implement more).

### D2 — `register_knowledge_store(name, factory)` plugin hook

A plugin contributes a named backend; `factory(config) -> KnowledgeBackend | None`:

```python
def register(registry):
    registry.register_knowledge_store("pgvector", build_pgvector_store)
```

Collected by the loader (`PluginLoadResult.knowledge_stores`, collision-guarded).

### D3 — `knowledge.backend` config selector + degrade-safe wiring

`knowledge.backend: "<name>"` selects a registered backend (default `""` = built-in
SQLite/Hybrid). `_build_knowledge_store` builds the default first (it's the collision-check
toolset's binding and the fallback); after plugins load, a small helper
(`_apply_plugin_knowledge_backend`) swaps in the selected plugin backend — and on `None` /
exception / an unregistered name **keeps the built-in store** (the same degrade-safe
principle as the FTS5 fallback: never KB-less by surprise). Applied at both init and the
live-reload path.

## Consequences

- A fork drops in **pgvector / Qdrant / Chroma / a managed vector DB** as a *plugin*
  (`register_knowledge_store`), no core edit — same pattern as goal verifiers (0028),
  chat surfaces (0029), console views (0026).
- The built-in SQLite/Hybrid store is the **default reference** and the fallback; nothing
  changes for forks that don't set `knowledge.backend`.
- The store interface is now explicit (`KnowledgeBackend`), so a backend author knows the
  exact surface to implement.

## Out of scope / future

- **The embedder** stays gateway-routed (model-swappable via `embed_model`). A
  `register_embedder` hook (for an in-process embedder like fastembed/sentence-transformers,
  no gateway round-trip) is a natural follow-up but not part of this ADR.
- Retrieval **fusion** (RRF) lives in the backend now — a custom backend owns its own
  ranking, so no separate seam is needed.

See ADR [0021](./0021-agent-memory-architecture.md) (memory architecture + embeddings),
[0019](./0019-plugin-config-settings-secrets.md) (plugin config/secrets/settings).
