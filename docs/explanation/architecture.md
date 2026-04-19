# Architecture

## The layers

```
┌──────────────┐     A2A JSON-RPC + SSE      ┌──────────────────┐
│   Consumer   │ ──────────────────────────▶ │  a2a_handler.py  │
│  (any A2A    │                             │  (FastAPI app)   │
│   client)    │ ◀──── cost-v1 DataPart ─────│                  │
└──────────────┘                             └────────┬─────────┘
                                                      │ submits message
                                                      ▼
                                            ┌──────────────────┐
                                            │  server.py       │
                                            │  _chat_langgraph │
                                            │  _stream         │
                                            └────────┬─────────┘
                                                      │ astream_events(v2)
                                                      ▼
                                            ┌──────────────────┐
                                            │  graph/agent.py  │
                                            │  (LangGraph      │
                                            │   create_agent)  │
                                            └────────┬─────────┘
                                                      │ tool calls +
                                                      │ chat completions
                                                      ▼
                                            ┌──────────────────┐
                                            │  LiteLLM gateway │
                                            │  (OpenAI-compat) │
                                            └──────────────────┘
```

Each arrow is a deliberate boundary.

## Why A2A handler is its own layer

A2A is a protocol, not a library. The handler owns:

- JSON-RPC 2.0 envelope handling
- SSE frame assembly with `kind` discriminators
- Task lifecycle state machine (SUBMITTED → WORKING → COMPLETED/FAILED/CANCELED)
- Push notification delivery + retry + SSRF guarding
- Extension extraction (cost-v1, worldstate-delta-v1)
- Dual token-shape parsing for `PushNotificationConfig`

The LangGraph runtime has no idea any of this exists. It sees a message, runs a tool loop, produces output. That means:

- If LangGraph's API changes, the A2A handler doesn't break.
- If A2A's spec changes, only this file changes.
- Tests for the protocol are isolated from tests for the agent.

## Why LangGraph owns the tool loop

LangGraph's `create_agent` gives you:

- Auto-generated system prompts that include tool schemas
- Structured tool-call emission (no "parse the model's text to extract tool intent")
- Middleware hooks (before_model, after_model, before_tool, after_tool) for tracing, auditing, knowledge injection
- Subagent delegation via the `task` tool, inheriting the parent's context

The template's middleware chain (`_build_middleware` in `graph/agent.py`) is ordered:

1. **KnowledgeMiddleware** (optional) — injects retrieved context before each LLM call; also loads prior session summaries from `/sandbox/memory/` as a `<prior_sessions>` block
2. **AuditMiddleware** — records every tool call to JSONL + Langfuse
3. **MemoryMiddleware** (optional) — persists session summaries to `/sandbox/memory/` on session end
4. **MessageCaptureMiddleware** — captures `message()` tool calls for later retrieval

Middleware order matters. Knowledge must run before audit (so the injected context is captured). Message capture runs last so every upstream transformation is already applied.

## Session memory

Memory is **enabled by default** (`middleware.memory: true` in `langgraph-config.yaml`). At session end `MemoryMiddleware` writes a JSON summary to `/sandbox/memory/`. On the next session, `KnowledgeMiddleware.load_memory()` reads the 10 most recent summaries and injects them as a `<prior_sessions>` XML block into the system prompt context, giving the agent continuity across restarts without any external store.

**Token budget:** the prior-sessions block is capped at 2 000 tokens (character approximation: chars ÷ 4). Oldest sessions are dropped first when the budget is exceeded.

**Disabling memory:** set `middleware.memory: false` in your fork's config, or set `PROTOAGENT_DISABLE_MEMORY=1` in the environment to suppress disk writes without changing the config.

**Persistence across container restarts:** mount a volume at `/sandbox/memory/`. Without a volume the directory is ephemeral and summaries are lost on container stop.

## Why LiteLLM sits between the agent and models

See [LiteLLM gateway](/explanation/litellm-gateway) for the full rationale. The short version: swapping models should be a one-line gateway config change, not a code change in every agent.

## Why streaming specifically this way

`_chat_langgraph_stream` in `server.py` consumes `astream_events(v2)` and yields structured frames: `tool_start`, `tool_end`, `usage`, `done`. The A2A handler then translates those into A2A SSE frames.

This extra layer of indirection exists because:

- A2A consumers want a stable frame vocabulary (`kind: "status-update"` with `taskId`, not LangGraph event names)
- The template needs to capture `on_chat_model_end` for cost-v1 emission — that event doesn't appear in A2A
- The agent might use the streaming output differently internally (e.g. buffering for `<scratch_pad>` / `<output>` extraction) than what consumers see

If you strip the indirection, you'd need to push A2A concerns up into LangGraph and LangGraph concerns down into the A2A handler. Both bad.

## The `_build_agent_card` reality

The agent card is just a JSON blob. Nothing on the server side reads it — it's declarative, for consumers only. That's why [adding a skill](/guides/add-a-skill) requires updating both the card AND the system prompt: the card tells callers what's possible, the prompt tells the LLM how to behave when it sees a matching request.

If you declare a skill on the card but don't teach the LLM about it, A2A callers can dispatch to it but the agent will treat it like a normal chat message. Debugging that mismatch is unpleasant.

## Related

- [A2A protocol](/explanation/a2a-protocol) — why the handler looks this way
- [Output protocol](/explanation/output-protocol) — why the streaming layer does that specific dance
- [Cost & trace](/explanation/cost-and-trace) — why `on_chat_model_end` matters
