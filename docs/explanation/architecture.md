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

## Security

Three independent layers defend the A2A surface. Each can be enabled or left open for local dev, but production forks should enable all three.

**Bearer authentication** — `a2a_handler.py` reads `A2A_AUTH_TOKEN` at startup. When set, every A2A route (`/a2a`, `message/send`, `tasks/*`, and SSE streaming endpoints) requires `Authorization: Bearer <token>`. Comparison uses `hmac.compare_digest` so timing analysis can't leak the token. When set, the agent card advertises `securitySchemes.bearer` so consumers know to present credentials.

**Audit redaction** — `graph/middleware/redaction.py` scrubs credentials before anything is written to `audit.jsonl` or emitted as a Langfuse span attribute. Patterns covered: `Authorization: Bearer ...`, OpenAI-style `sk-...` keys, generic `api_key=...` forms, and nested dicts keyed by well-known env var names (`OPENAI_API_KEY`, `LANGFUSE_SECRET_KEY`, `A2A_AUTH_TOKEN`, etc.). This closes the class of bugs where a tool returns a secret in its payload and it leaks into the audit trail or trace.

**Origin verification** — SSE and WebSocket connections to streaming endpoints check the `Origin` header against `A2A_ALLOWED_ORIGINS`. Without this, anyone who can reach the A2A endpoint can drain another session's events if they guess the task ID. Unset logs a WARNING at startup and accepts all origins (template default); setting `*` explicitly disables the check without the warning.

The three layers compose: auth proves the caller is known, redaction ensures the audit trail won't leak secrets even if a tool misbehaves, origin verification prevents cross-origin SSE drain. Turn them all on — none substitute for the others.

## Skill loop

The `task()` subagent tool captures successful workflows as **skill-v1 artifacts** so the agent can reuse them on similar future problems. This is the "gets better the longer it runs" property you see in systems like Hermes Agent, adapted to protoAgent's A2A-native shape.

Four pieces:

1. **Emission** — when a subagent completes successfully and `task(..., emit_skill=True)` was called (and the subagent's config has `allow_skill_emission: true`), `graph/extensions/skills.py` serializes a `SkillV1Artifact` (name, description, prompt_template, tools_used, source_session_id) into a ContextVar.
2. **Collection** — the A2A handler reads the ContextVar at task completion and appends a DataPart with `mimeType: application/vnd.protolabs.skill-v1+json` to the terminal artifact. See the [skill-v1 extension reference](/reference/extensions#skill-v1).
3. **Indexing** — `graph/skills/index.py` stores emitted skills in a SQLite database at `/sandbox/skills.db` with an FTS5 virtual table over `name + description + prompt_template`. The DB auto-initializes on first write; an empty DB is a no-op for queries.
4. **Retrieval** — `KnowledgeMiddleware.load_skills(query)` returns the top-k matches (default 5, BM25-ranked) for the current user message + recent context, injected as a `<learned_skills>` block in the system prompt. Same 2 K-token budget discipline as `<prior_sessions>`.

**Curation** — `python -m graph.skills.curator` runs a periodic sweep that deduplicates near-identical skills and decays confidence 50 % every 90 days of idleness. Skills below 0.2 confidence are pruned. Run it on a cron or let operators trigger it manually — no automatic scheduling in the template.

**Opting out per subagent** — set `allow_skill_emission: false` in `graph/subagents/config.py` for subagents whose runs shouldn't be captured (e.g. sensitive ones). The `disallowed_tools` mechanism is unaffected — skill emission is orthogonal to tool access control.

**Why SQLite + FTS5** — the index lives inside the container, survives restarts if `/sandbox` is volume-mounted, handles tens of thousands of skills without a separate service, and the fts5 virtual table gives BM25 ranking without embedding model overhead. You can swap in a vector store later if recall beats keyword BM25 for your domain; the `KnowledgeMiddleware.load_skills()` seam is the single swap point.

See the [skill loop tutorial](/tutorials/skill-loop) for the end-to-end walkthrough.

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
