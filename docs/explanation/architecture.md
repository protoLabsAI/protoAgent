# Architecture

## The layers

```
┌──────────────┐     A2A JSON-RPC + SSE      ┌──────────────────┐
│   Consumer   │ ──────────────────────────▶ │  A2A handler     │
│  (any A2A    │                             │  (FastAPI app)   │
│   client)    │ ◀──── cost-v1 DataPart ─────│                  │
└──────────────┘                             └────────┬─────────┘
                                                      │ submits message
                                                      ▼
                                            ┌──────────────────┐
                                            │  server/chat.py  │
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
- If A2A's spec changes, only this layer changes (the `server/a2a.py` + `a2a_executor.py` + `a2a_stores.py` modules).
- Tests for the protocol are isolated from tests for the agent.

## Why LangGraph owns the tool loop

LangGraph's `create_agent` gives you:

- Auto-generated system prompts that include tool schemas
- Structured tool-call emission (no "parse the model's text to extract tool intent")
- Middleware hooks (before_model, after_model, before_tool, after_tool) for tracing, auditing, knowledge injection
- Subagent delegation via the `task` tool, inheriting the parent's context

The template's middleware chain (`_build_middleware` in `graph/agent.py`) is ordered (optional links are config-gated):

1. **PromptCacheMiddleware** — sets Anthropic cache breakpoints on the stable system+tools prefix (the knowledge context is delivered just after it)
2. **EnforcementMiddleware** (optional) — capability/effect-domain enforcement
3. **KnowledgeMiddleware** (optional) — injects retrieved knowledge + human-authored skills before each LLM call; also loads prior session memory
4. **ToolDeferralMiddleware** (optional) — progressive tool disclosure (ADR 0005)
5. **AuditMiddleware** — records every tool call to JSONL + Langfuse
6. **SessionSummaryMiddleware** (optional) — persists a session summary on session end (read back as `<prior_sessions>`)
7. **CountingSummarizationMiddleware** (optional) — context compaction with a Prometheus counter (ADR 0006)
8. **ModelFallbackMiddleware** (optional) — retry on fallback models (`routing.fallback_models`)
9. **MessageCaptureMiddleware** — captures `message()` tool calls; runs last so every upstream transformation is already applied

Order matters: prompt-cache + knowledge run before audit (so injected context is captured), and message capture runs last.

## Session memory

Memory is **enabled by default** (`middleware.memory: true` in `langgraph-config.yaml`). At session end `SessionSummaryMiddleware` writes a JSON summary to `/sandbox/memory/`. On the next session, `KnowledgeMiddleware.load_memory()` reads the 10 most recent summaries and injects them as a `<prior_sessions>` XML block into the system prompt context, giving the agent continuity across restarts without any external store.

**Token budget:** the prior-sessions block is capped at 2 000 tokens (character approximation: chars ÷ 4). Oldest sessions are dropped first when the budget is exceeded.

**Disabling memory:** set `middleware.memory: false` in your fork's config, or set `PROTOAGENT_DISABLE_MEMORY=1` in the environment to suppress disk writes without changing the config.

**Persistence across container restarts:** mount a volume at `/sandbox/memory/`. Without a volume the directory is ephemeral and summaries are lost on container stop.

## Security

Three independent layers defend the A2A surface. Each can be enabled or left open for local dev, but production forks should enable all three.

**Bearer authentication** — `a2a_auth.py` reads `A2A_AUTH_TOKEN` at startup. When set, every A2A route (`/a2a`, `message/send`, `tasks/*`, and SSE streaming endpoints) requires `Authorization: Bearer <token>`. Comparison uses `hmac.compare_digest` so timing analysis can't leak the token. When set, the agent card advertises `securitySchemes.bearer` so consumers know to present credentials.

**Audit redaction** — `graph/middleware/redaction.py` scrubs credentials before anything is written to `audit.jsonl` or emitted as a Langfuse span attribute. Patterns covered: `Authorization: Bearer ...`, OpenAI-style `sk-...` keys, generic `api_key=...` forms, and nested dicts keyed by well-known env var names (`OPENAI_API_KEY`, `LANGFUSE_SECRET_KEY`, `A2A_AUTH_TOKEN`, etc.). This closes the class of bugs where a tool returns a secret in its payload and it leaks into the audit trail or trace.

**Origin verification** — SSE and WebSocket connections to streaming endpoints check the `Origin` header against `A2A_ALLOWED_ORIGINS`. Without this, anyone who can reach the A2A endpoint can drain another session's events if they guess the task ID. Unset logs a WARNING at startup and accepts all origins (template default); setting `*` explicitly disables the check without the warning.

The three layers compose: auth proves the caller is known, redaction ensures the audit trail won't leak secrets even if a tool misbehaves, origin verification prevents cross-origin SSE drain. Turn them all on — none substitute for the others.

## Skill loop

A **skill** teaches the agent how and when to run a recurring workflow. Available skills are advertised to the agent every turn as a lightweight index; the agent loads a skill's full procedure only when it judges one fits the task, so it reuses proven approaches on similar future problems — the "gets better the longer it runs" property, adapted to protoAgent's A2A-native shape.

Three pieces ([progressive disclosure, ADR 0060](/adr/0060-skill-progressive-disclosure)):

1. **Authoring** — a skill is an [AgentSkills `SKILL.md`](/guides/skills) folder. You drop them in by hand, and the agent can author its own from a proven workflow via the `/distill` subagent (it writes a new `SKILL.md`). All land in the index as `source=disk`.
2. **Indexing** — `graph/skills/index.py` is a SQLite/FTS5 store at `/sandbox/skills.db` (→ `~/.protoagent/skills.db` when `/sandbox` isn't writable). `SKILL.md` folders are re-seeded on every boot; console edits (Agent → Skills) index live.
3. **Disclosure** — `KnowledgeMiddleware` injects an always-on `<available_skills>` block listing up to `skills.top_k` skills' `{name, description}` (recency-ordered, query-independent), and the agent calls the `load_skill(name)` tool to pull one skill's full procedure on demand (visible as a tool card). This replaced per-turn BM25 retrieval of full skill bodies. (The index is wired into `KnowledgeMiddleware` via `create_agent_graph`'s `skills_index`.)

**Curation** — `python -m graph.skills.curator` runs a periodic sweep that deduplicates near-identical skills and decays confidence 50 % every 90 days of idleness. Skills below 0.2 confidence are pruned. `disk` skills are **pinned** (re-seeded from `SKILL.md` files, not curated). Run it on a cron or let operators trigger it manually — no automatic scheduling in the template.

**Why SQLite + FTS5** — the index lives inside the container, survives restarts if `/sandbox` is volume-mounted, handles tens of thousands of skills without a separate service, and the fts5 virtual table backs both the `list_skills`/`get_skill` lookups and the curator's queries without embedding-model overhead. `SkillsIndex.skill_summaries()` (the always-on index) and `get_skill()` (on-demand body) are the single read seam — swap the store there if your domain outgrows keyword search.

## Extending the agent (tools, skills, plugins)

Beyond the shipped tools, three opt-in seams add capability to a *running* agent without forking — the architecture recorded in [ADR 0001](/adr/):

- **Tools enter via one list.** `create_agent_graph` assembles `get_all_tools()` (built-in) plus an `extra_tools` argument, then hands the combined set to the LangGraph loop. Both external sources below feed `extra_tools`, so they're indistinguishable to the model and inherit the same Audit/Langfuse middleware.
- **MCP** (`tools/mcp_tools.py`) — configured [Model Context Protocol](/guides/mcp) servers (stdio / streamable-HTTP) are connected via `langchain-mcp-adapters`; their tools are discovered at graph-build time, namespaced `<server>__<tool>`, and appended to `extra_tools`. The client is stateless (a fresh session per call), so discovery happens once and tools are event-loop-agnostic.
- **Plugins** (`graph/plugins/`: `loader`, `registry`, `manifest`, `host`, `pconfig`) — drop-in packages (`protoagent.plugin.yaml` + `register(registry)`) that contribute, via the registry, **tools** (→ `extra_tools`), bundled **`SKILL.md`** dirs (→ the skill index), FastAPI **routers** (mounted under `/plugins/<id>`), background **surfaces** (lifecycle-managed ingress like the Discord gateway), **subagents** (→ `SUBAGENT_REGISTRY`), and managed **MCP servers** (→ `mcp.servers[]` factory), plus their own **config / secrets / Settings** claimed as a top-level YAML section (ADR 0018/0019). A surface/route reaches the agent + event bus + live config via the plugin **host** (`registry.host`: `invoke` / `publish` / `subscribe` / `on` / `config` / `apply_settings`). The bus is a decoupled **topic pub/sub** (ADR 0039) — plugins `emit`/`on` by namespaced topic, never importing each other. They run **in-process** with the agent's privileges, so a third-party plugin is disabled by default. Bundled first-party plugins (e.g. `plugins/telegram`, `plugins/github`) ship this way — **not** wired into the core `server/` package; richer integrations like the **Discord** ingress surface, a **Google** Gmail/Calendar managed MCP server, or a **Slack** surface install at runtime as external plugins from their own repos (ADR 0058). See [Plugins](/guides/plugins).

All are surfaced in `GET /api/runtime/status` (`skills`, `mcp`, `plugins` — with per-plugin route/surface/subagent counts) and load best-effort — a bad skill/server/plugin is logged and skipped, never fatal. Untrusted third-party tools belong on MCP (out-of-process) rather than in-process plugins.

## Why LiteLLM sits between the agent and models

See [LiteLLM gateway](/explanation/litellm-gateway) for the full rationale. The short version: swapping models should be a one-line gateway config change, not a code change in every agent.

## Why streaming specifically this way

`_chat_langgraph_stream` in `server/chat.py` consumes `astream_events(v2)` and yields structured frames: `tool_start`, `tool_end`, `usage`, `done`. The A2A executor (`a2a_executor.py`) then translates those into A2A SSE frames.

This extra layer of indirection exists because:

- A2A consumers want a stable frame vocabulary (`kind: "status-update"` with `taskId`, not LangGraph event names)
- The template needs to capture `on_chat_model_end` for cost-v1 emission — that event doesn't appear in A2A
- The agent might use the streaming output differently internally (e.g. buffering for `<scratch_pad>` / `<output>` extraction) than what consumers see

If you strip the indirection, you'd need to push A2A concerns up into LangGraph and LangGraph concerns down into the A2A handler. Both bad.

## The `_build_agent_card_proto` reality

The agent card is just a JSON blob. Nothing on the server side reads it — it's declarative, for consumers only. That's why [adding a skill](/guides/add-a-skill) requires updating both the card AND the system prompt: the card tells callers what's possible, the prompt tells the LLM how to behave when it sees a matching request.

If you declare a skill on the card but don't teach the LLM about it, A2A callers can dispatch to it but the agent will treat it like a normal chat message. Debugging that mismatch is unpleasant.

## Related

- [A2A protocol](/explanation/a2a-protocol) — why the handler looks this way
- [Output protocol](/explanation/output-protocol) — why the streaming layer does that specific dance
- [Cost & trace](/explanation/cost-and-trace) — why `on_chat_model_end` matters
