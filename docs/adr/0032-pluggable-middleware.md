# ADR 0032 — Pluggable middleware

**Status:** Accepted

## Context

Middleware was the **last core extension point a fork still had to edit core to use**.
The agent's per-turn hook layer — `before_model` / `after_model` / `wrap_tool_call`
(LangGraph `AgentMiddleware`) — is assembled as a static list in
`graph/agent.py::_build_middleware` (prompt-cache, knowledge, enforcement, deferral,
audit, memory, ingest, compaction, model-fallback, message-capture). Everything else a
fork needs is plugin-contributable (tools, subagents, MCP servers, knowledge backends,
goal verifiers, …), but a fork that wanted a custom per-turn behavior had to patch
`graph/agent.py` or `a2a_executor.py`.

Concretely: **roxy** (the canonical operator fork) carried a core edit in
`a2a_executor.py` — a project-**scope banner** that reads the A2A request metadata and
injects a per-turn directive. That kept roxy from being a pure-plugin fork.

Two gaps blocked closing it: (1) no `register_middleware`, and (2) per-request metadata
stopped at `_chat_langgraph_stream` — it never reached middleware.

## Decision

1. **`registry.register_middleware(factory)`** — a plugin contributes a LangGraph
   `AgentMiddleware`. `factory(config) -> AgentMiddleware | None` (mirrors
   `register_knowledge_store` / `register_embedder`); returning `None` opts out. The
   loader collects factories into `PluginLoadResult.middleware`; `agent_init` resolves
   them to instances (best-effort — a raising/None factory is skipped + logged) and
   threads them as `create_agent_graph(extra_middleware=…)`. `_build_middleware` appends
   them **after the core chain but before `MessageCaptureMiddleware`**, so their hooks run
   and the turn is still captured. Applies to the lead agent (subagents keep their lean
   built-in chain).

2. **Per-request metadata via a contextvar.** `graph/middleware/request_context.py`
   exposes `current_request_metadata()` (the merged A2A request metadata for the in-flight
   turn) backed by a contextvar, bound by `request_metadata_scope(...)` in the A2A stream
   (`_chat_langgraph_stream`, alongside `trace_session`). Mirrors
   `tracing.current_session_id`. Middleware — core or plugin — reads request-scoped data
   without touching the executor or the graph state schema.

Result: a fork's per-turn directive (roxy's scope banner) becomes a ~15-line plugin
`AgentMiddleware` reading `current_request_metadata()` — **zero core edits**.

## Consequences

- Plugin middleware runs **in-process with the agent's privileges** (like all plugins) —
  opt-in, trust-gated.
- Ordering is fixed (core chain → plugin middleware → message-capture). A priority/position
  hint is a deferred follow-up if a plugin needs to wrap the model *outermost*.
- The contextvar is set only on the A2A stream path (where request metadata exists);
  non-A2A invokes see `{}`.
- Subagents don't get plugin middleware (kept minimal by design).
