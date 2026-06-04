# Configuration

`config/langgraph-config.yaml` is the runtime config. Loaded at server boot by `graph/config.py::LangGraphConfig.from_yaml()`. All fields have defaults; the YAML only needs to override what's changing.

**Template vs. live file.** The repo tracks `config/langgraph-config.example.yaml` (the shipped template, with defaults + comments). The live `config/langgraph-config.yaml` is **untracked** — it's per-deployment state, written by the setup wizard / settings drawer. On first run the server copies the template into place (`config_io.ensure_live_config`), so edits never dirty a tracked file. Secrets are split out further into `config/secrets.yaml` (see [Secrets](#secrets)).

## Full example

```yaml
model:
  provider: openai
  name: protolabs/reasoning
  api_base: http://gateway:4000/v1
  api_key: ""
  temperature: 0.2
  max_tokens: 32768
  max_iterations: 50

subagents:
  researcher:
    enabled: true
    tools:
      - current_time
      - web_search
      - fetch_url
      - memory_recall
      - memory_list
    max_turns: 40

middleware:
  knowledge: true
  audit: true
  memory: true
  scheduler: true

knowledge:
  db_path: /sandbox/knowledge/agent.db
  embed_model: nomic-embed-text
  top_k: 5
```

## `model`

| Key | Default | What |
|---|---|---|
| `provider` | `openai` | LangChain LLM provider. The template's `graph/llm.py` only uses `openai` (via LiteLLM gateway). |
| `name` | `protolabs/reasoning` | Gateway alias or direct model name. |
| `api_base` | `http://gateway:4000/v1` | OpenAI-compatible endpoint. |
| `api_key` | `""` | **Secret — not stored here.** Managed in the untracked `config/secrets.yaml` (see [Secrets](#secrets)); falls back to the `OPENAI_API_KEY` env var. |
| `temperature` | `0.2` | Sampling temperature. |
| `max_tokens` | `32768` | Per-call output cap. 32k headroom for the Qwen models we run. |
| `max_iterations` | `50` | Upper bound on tool-call loops per task. |
| `top_p` | _(unset)_ | Nucleus sampling. Standard OpenAI param; sent only when set. |
| `presence_penalty` | _(unset)_ | Standard OpenAI param; sent only when set. |
| `top_k` | `-1` | Top-k sampling. Rides `extra_body` (vLLM-style gateways). `-1`/negative = gateway default. |
| `repetition_penalty` | _(unset)_ | Rides `extra_body`; sent only when set. |
| `chat_template_kwargs` | _(unset)_ | Dict passed via `extra_body` to the vLLM renderer, e.g. `{preserve_thinking: true}` to keep historical `<think>`/`<scratch_pad>` blocks across turns. |

All sampling params are optional — omit to use the gateway / model-card defaults. `temperature`, `max_tokens`, `top_p`, and `presence_penalty` are standard OpenAI fields; `top_k`, `repetition_penalty`, and `chat_template_kwargs` are sent via `extra_body` for vLLM-compatible gateways.

## Secrets

Two **core** fields are secrets and are **never written to the tracked config YAML**: the model `api_key` and the A2A `auth.token`. (Plugins may declare more — e.g. `discord.bot_token`, `google.client_secret` — which are routed and stripped the same way via a dynamic `secret_paths()`; ADR 0019.) The setup wizard and settings drawer persist them to an **untracked** sibling file, `config/secrets.yaml` (gitignored, dockerignored, written `0600`):

```yaml
# config/secrets.yaml — never committed
model:
  api_key: sk-...
auth:
  token: bearer-...
```

`LangGraphConfig.from_yaml` overlays this file on top of the main config at load time. Precedence for each secret: **`secrets.yaml` → main YAML value → env var** (`OPENAI_API_KEY` / `A2A_AUTH_TOKEN`). So env-injected deployments (e.g. `infisical run`) work unchanged — just leave `secrets.yaml` absent. Every config save also strips any secret keys the main YAML might still carry, so a checkout converges to secret-free. The `/api/config` endpoint redacts both fields to `""`; runtime status reports only whether a key is set (`model.api_key_configured`), never the value.

## `subagents`

One entry per subagent name. Each entry matches a `SubagentConfig` in `graph/subagents/config.py` and a `SubagentDef` field in `LangGraphConfig`.

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | If false, the subagent is still registered but dispatches return "disabled" errors. |
| `tools` | `[]` | Allowlist. Tool names not listed here are invisible to this subagent. |
| `max_turns` | `30` | Recursion cap. |

Two `subagents`-block keys govern **fan-out** via the `task_batch` tool (concurrent delegation):

| Key | Default | What |
|---|---|---|
| `max_concurrency` | `4` | Cap on in-flight subagents per `task_batch` call (protects the gateway + context budget). |
| `output_truncate` | `6000` | Per-subagent returned-text cap (chars) under `task_batch`, so a wide fan-out can't blow the parent context. Single `task` is unbounded. |

```yaml
subagents:
  max_concurrency: 4
  output_truncate: 6000
  researcher:
    enabled: true
    tools: [...]
```

Adding a new subagent name to the YAML requires matching entries in `graph/subagents/config.py::SUBAGENT_REGISTRY`, `graph/config.py::LangGraphConfig`, and the `from_yaml()` loop. See [Configure subagents](/guides/subagents).

## `middleware`

| Key | Default | What |
|---|---|---|
| `knowledge` | `true` | Inject retrieved knowledge into state before LLM calls. Backed by the bundled `KnowledgeStore` (sqlite + FTS5). Set `false` for a stateless agent. |
| `audit` | `true` | Append every tool call to `/sandbox/audit/audit.jsonl`. |
| `memory` | `true` | Persist a session summary on terminal turn and asynchronously index conversation findings under `domain='finding'`. |
| `scheduler` | `true` | Wire the bundled scheduler backend (local sqlite, or `WorkstaceanScheduler` when env vars are set). Drops the `schedule_task` / `list_schedules` / `cancel_schedule` tools from the agent loop when `false`. Has the same effect as `SCHEDULER_DISABLED=1` — but `middleware.scheduler: false` is the canonical opt-out (drawer/wizard editable, survives restarts), while the env var is a runtime escape hatch for fleet operators who can't edit YAML in the moment. |
| `enforcement` | `false` | Opt-in safety gate that blocks tool calls **before** they execute (see `enforcement` block below). No-op unless a deny list or rate limit is configured. |
| `ingest` | `false` | Opt-in: capture tool output into the KB **after** execution (see `ingest` block below). |

## `enforcement`

Optional pre-execution gate (`graph/middleware/enforcement.py`). Only read when `middleware.enforcement: true`. Blocked calls return a `ToolMessage` explaining the denial (the model reads it and adapts) instead of running the tool. Forks needing richer policy (scope/cost/etc.) can attach a `predicate(tool_name, args) -> reason|None` in code.

```yaml
middleware:
  enforcement: true
enforcement:
  disallowed_tools: [fetch_url]          # exact names never allowed
  rate_limits:
    web_search: { max: 20, window_seconds: 60 }
```

| Key | Default | What |
|---|---|---|
| `disallowed_tools` | `[]` | Tool names that are always blocked. |
| `rate_limits` | `{}` | Per-tool sliding-window limit: `{max, window_seconds}`. |

## `ingest`

Optional post-execution capture (`graph/middleware/knowledge_ingest.py`). Only read when `middleware.ingest: true`. After a tool runs, its output is stored in the KB under `domain='finding'` (recall-able later). Fire-and-forget — never breaks the loop. With no extractor it stores the raw (truncated) output; forks attach `extractor(tool_name, output) -> list[str]` in code (e.g. a small LLM) for distilled findings.

```yaml
middleware:
  ingest: true
ingest:
  tools: [web_search, fetch_url]   # empty/omitted = capture all tools
```

| Key | Default | What |
|---|---|---|
| `tools` | `[]` | Restrict capture to these tool names (empty = all). |

## `prompt_cache`

`PromptCacheMiddleware` (`graph/middleware/prompt_cache.py`) does two things at the model-call boundary: (1) **delivers** the volatile knowledge/skills/hot-memory context that `KnowledgeMiddleware` produces — `create_agent` builds a static system prompt and doesn't read the `context` state key, so this is what actually gets that context to the model; (2) sets Anthropic **`cache_control`** on the stable system-prompt prefix, with the volatile context placed *after* the breakpoint so it never invalidates the cached prefix.

Caching is gated to Anthropic-family models (safe no-op elsewhere); **context delivery happens regardless**, so the middleware is always wired.

```yaml
prompt_cache:
  enabled: true     # caching half (delivery is unconditional)
  ttl: "5m"         # "5m" ephemeral, or "1h" persistent (agent turns exceed 5m)
  force: false      # cache even when the model name doesn't look Anthropic
                    # (use when your gateway alias hides a Claude model)
  warm:             # cache-warming heartbeat (off by default)
    enabled: false
    interval_seconds: 3300   # 55m — just under the "1h" tier
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Apply `cache_control` (Anthropic). No-op on non-Anthropic models. |
| `ttl` | `"5m"` | Cache tier: `5m` (ephemeral) or `1h` (persistent). |
| `force` | `false` | Bypass the Anthropic-name heuristic (opaque gateway aliases). |
| `warm.enabled` | `false` | Run a background heartbeat (`graph/cache_warmer.py`) that periodically reproduces the cached system prefix so the **first** request after an idle gap hits a warm cache instead of a full miss. |
| `warm.interval_seconds` | `3300` | Heartbeat period. Set just under `ttl` (default 55m for the `1h` tier). |

**When to enable `warm`:** sporadic but latency-sensitive traffic on the `1h` tier — the ~1-token ping per interval is cheap relative to a cold miss on a multi-thousand-token prefix while a user waits. Leave it **off** for steady traffic (the cache stays warm on its own — warming is then pure cost) and on non-Anthropic models (nothing to warm; the warmer no-ops at start unless `force` is set). It runs as its own asyncio task (started/stopped with the server), **not** through the scheduler — the scheduler fires full agent turns, the wrong primitive for a keep-alive.

## `compaction`

Wires langchain's `SummarizationMiddleware` to summarize old history near the context limit (enables long-horizon runs; we otherwise only cap via `max_iterations`). Opt-in.

```yaml
compaction:
  enabled: true
  trigger: "fraction:0.8"   # or "tokens:120000" / "messages:80"
  keep_messages: 20          # most-recent messages kept verbatim
  model: ""                  # blank = summarize with the main model; or a cheaper one
```

## `execute_code`

Opt-in **programmatic tool calling** (`tools/execute_code.py`). Adds an `execute_code` tool: the model writes one Python script that calls several tools, loops/filters/composes their results in code, and `print()`s only the final answer — collapsing a long tool-call chain into a single turn (the model reads just the stdout, not every intermediate payload).

The script runs in a **child process** with a **scrubbed environment** (only `PATH` + the bridge fds — no gateway keys / auth tokens) and a **hard timeout**. Tools are invoked back in the **parent** over an fd-based RPC bridge, so they run with the parent's credentials, audit, and trace context; the child only orchestrates. Inside the script, tools are reached via an injected `tools` object (`tools.web_search(query=...)`). The `execute_code` tool never exposes itself, so scripts can't recurse.

```yaml
execute_code:
  enabled: false           # OFF by default — runs model-authored code
  timeout: 30.0            # seconds before the child process is killed
  tools: []                # allowlist; empty = all tools except execute_code
  output_truncate: 6000    # cap on returned stdout (chars)
```

| Key | Default | What |
|---|---|---|
| `enabled` | `false` | Register the `execute_code` tool. |
| `timeout` | `30.0` | Wall-clock limit; the child is killed past it. |
| `tools` | `[]` | Tool-name allowlist exposed to scripts (empty = all but `execute_code`). |
| `output_truncate` | `6000` | Max returned stdout chars. |

> **Security:** subprocess + env-scrub + timeout is *isolation, not a true sandbox* — the child can still touch the filesystem and network as the server user. Enable only for trusted-model output or inside a hardened container (seccomp / read-only FS / network policy). Narrow `tools` to the minimum the workload needs.

## `tools`

**Deferred tools** — progressive tool disclosure for high tool counts ([ADR 0005](../adr/0005-tool-pollution-and-progressive-disclosure.md)). When enabled, only a small base set + a `search_tools` meta-tool are shown to the model each turn; the rest stay bound (callable) but their schemas are withheld until the agent calls `search_tools` to load them. This cuts the per-turn tool-schema footprint and improves selection accuracy once you routinely exceed ~15 tools.

```yaml
tools:
  disabled: []              # core tool names to DROP (a fork's denylist)
  deferred:
    enabled: false          # OFF by default — the full tool set is shown
    keep: []                # always-on tool names; empty = built-in base
```

| Key | Default | What |
|---|---|---|
| `disabled` | `[]` | Core tool names to **drop** from the agent without editing `get_all_tools` — a fork keeps what it wants by listing the rest. Live-reloadable. Plugins still ADD tools on top (see [Plugins](/guides/plugins)). ([ADR 0005](../adr/0005-tool-pollution-and-progressive-disclosure.md)) |
| `deferred.enabled` | `false` | Withhold most tool schemas; expose them via `search_tools`. |
| `deferred.keep` | `[]` | Tool names always shown. Empty → built-in base (keyless core + `task`/`task_batch`/`run_workflow`/`save_workflow` + `search_tools`). `search_tools` is always kept regardless. |

Every tool remains **executable** even while deferred — `create_agent` registers all executors; deferral only trims what the model *sees* per turn. The agent loads tools by calling `search_tools("github pull request")`; matches stay available for the rest of the thread. Leave off unless you have a large catalog (e.g. a chatty MCP server) — for a handful of tools it adds a discovery hop for no benefit.

## `telemetry`

Local per-turn cost/latency rollup ([ADR 0006](../adr/0006-observability-and-the-self-improving-flywheel.md)). One row per terminal A2A turn (tokens incl. cache, USD cost, duration, LLM/tool call counts), queryable at `/api/telemetry/summary` + `/api/telemetry/recent`.

```yaml
telemetry:
  enabled: true                 # one cheap write per turn
  db_path: /sandbox/telemetry.db
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Write a per-turn row at terminal time. `false` → no store; endpoints return `{enabled:false}`. |
| `db_path` | `/sandbox/telemetry.db` | SQLite path; `/sandbox`→`~/.protoagent` fallback, instance-scoped (ADR 0004). |

## `filesystem`

Fenced multi-project filesystem toolset ([ADR 0007](../adr/0007-directory-aware-operator-agent.md)) — a generic primitive that gives the agent read/write/list/search + fenced command execution over a registry of project directories. It is **ON by default**, fenced to a default `workspace` dir when no explicit `projects` are set (override with `PROTOAGENT_WORKSPACE`). The capability a forked operator (e.g. "Roxy") composes into a multi-project manager — see the [operator-fork guide](../guides/operator-fork.md).

```yaml
filesystem:
  enabled: true                  # ON by default
  allow_run: true                # run_command available (ON); HITL-gated below
  run_requires_approval: true    # each run_command pauses for operator approval
  projects:
    - { name: orbis, path: /Users/kj/dev/ORBIS, write: false }   # read-only monitor
    - { name: pixelgen, path: /Users/kj/dev/pixelgen, write: true }
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Expose the fs tools (`list_projects`/`read_file`/`list_dir`/`find_files`/`search_files`/`write_file`/`edit_file`). Off → no fs tools. |
| `allow_run` | `true` | Also expose `run_command` (fenced `cwd`, but arbitrary argv — dual-use, like `execute_code`). |
| `run_requires_approval` | `true` | Each `run_command` call pauses for HITL operator approval (A2A `input-required`). Drop to `false` to let commands run unattended. |
| `projects` | `[]` | Managed workspaces: `{name, path, write}`. **Empty falls back to a default `workspace` dir** (so the tools are usable out of the box). **Every path is fenced under a project root** (`..`/symlink escapes refused); `write:false` makes a project read-only; invalid paths are skipped. |

**Security:** the project roots are the **hard fence** — every tool resolves paths under a root and refuses escapes; `write_file`/`edit_file` need `write:true`; the agent's own repo is not a project unless you add it. All mutations are audited. See ADR 0007 §4.

## `egress`

Deny-by-default outbound-host allowlist ([ADR 0008](../adr/0008-sandboxing-and-openshell.md)) enforced in `fetch_url` — the tool where the model picks an arbitrary host (the in-process exfiltration / SSRF vector). Also the single source of truth the OpenShell network policy is generated from (`scripts/gen_openshell_policy.py`).

```yaml
egress:
  allowed_hosts:
    - api.proto-labs.ai
    - "*.github.com"      # wildcard: apex + any subdomain
```

| Key | Default | What |
|---|---|---|
| `allowed_hosts` | `[]` | Hosts `fetch_url` may reach. **Empty = permissive** (off). When set, any other host is denied. `*.host` matches subdomains + apex; case-insensitive, port-agnostic. Hot-reloads. |

Covers `fetch_url` only; `execute_code`/`run_command` process-level egress is fenced by running under OpenShell (see [Sandboxing & egress](../guides/sandboxing.md)).

## `routing`

Wires langchain's `ModelFallbackMiddleware`: on a primary-model error, retry on each fallback model (same gateway) in order. Opt-in (empty = no fallback). `aux_model` is a separate, optional cheap/fast alias for non-reasoning calls.

```yaml
routing:
  fallback_models: [claude-haiku-4-5, gpt-5]
  aux_model: ""        # cheap/fast alias for summarization, goal-verify, subagent delegation
```

| Key | Default | What |
|---|---|---|
| `fallback_models` | `[]` | Models to retry on a primary-model error, in order (same gateway). Empty = no fallback. |
| `aux_model` | `""` | Single cheap/fast alias for non-reasoning calls (compaction summarizer, goal verifier, subagent delegation). Blank = everything runs on the main model; each path's own override still wins. |

## `goal`

**Goal mode** (`graph/goals/`) lets you give the agent a *testable outcome* it self-drives toward. After each terminal turn (the agent stops with a final answer), the goal's **verifier** decides whether it's met; if not, the agent is re-invoked with a continuation prompt — carrying the verifier's evidence and a running `<goal_plan>` checklist — until the verifier passes, the iteration budget runs out (`exhausted`), or the goal is flagged `unachievable` (a no-progress streak, or the model emitting `<goal_unachievable reason="…"/>`). Unlike a pure-LLM "are we done?" check, completion is backed by a real verifier.

The machinery is wired when `enabled`, but **no goal is active until one is set** via the `/goal` control message (works over A2A / Gradio / OpenAI-compat) or the `/api/goal/{session_id}` endpoints. State is persisted per session under `GOAL_PATH` → `/sandbox/goals` → `~/.protoagent/goals`.

```yaml
goal:
  enabled: true            # machinery available; no goal active until set
  max_iterations: 8        # continuation budget per goal
  no_progress_limit: 3     # identical verifier evidence N times -> unachievable
  eval_model: ""           # blank = main model (llm verifier / fuzzy goals)
  verify_timeout: 120      # seconds for command/test/ci verifiers
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Wire goal mode. No goal runs until set. |
| `max_iterations` | `8` | Max continuation turns before a goal is `exhausted`. |
| `no_progress_limit` | `3` | Same verifier reason+evidence this many times in a row → `unachievable`. |
| `eval_model` | `""` | Model for the `llm` verifier (blank = main model). |
| `verify_timeout` | `120` | Wall-clock seconds for `command`/`test`/`ci` verifiers. |

**Setting a goal** — `/goal <text>` (fuzzy, `llm`-verified) or a JSON spec:
```
/goal {"condition": "unit tests pass", "verifier": {"type": "test", "command": "python -m pytest -q"}}
```
`/goal` shows status; `/goal clear` (aliases: `stop`, `off`, `cancel`, `reset`, `none`) clears it.

**Verifier types** (`verifier.type`): `command` (exit 0 = met), `test` (command + surfaces the runner summary), `ci` (`gh pr checks <pr>` or latest run on `branch`), `data` (a file `contains` substring, or an `expr` over parsed JSON as `data`), `llm` (transcript judgment — fuzzy fallback).

> **Security:** `command`/`test`/`ci` verifiers execute on the server host. Setting a goal is an **operator** action — only accept goal specs from trusted input. See [Goal mode](/guides/goal-mode).

## `knowledge`

Only read when `middleware.knowledge` is `true`.

| Key | Default | What |
|---|---|---|
| `db_path` | `/sandbox/knowledge/agent.db` | SQLite file path. Falls back to `~/.protoagent/knowledge/agent.db` automatically when the configured path isn't writable (e.g. running locally without `/sandbox`). Override at runtime with `KNOWLEDGE_DB_PATH`. |
| `embed_model` | `nomic-embed-text` | Reserved for forks that bolt embeddings on top of the FTS5 baseline. The bundled store ignores it. |
| `top_k` | `5` | Results per query fed into state. |

The bundled store is sqlite + FTS5 (with an automatic LIKE fallback when FTS5 isn't available). One `chunks` table; the `domain` column distinguishes operator-set notes (`memory_ingest`), daily-log entries (`daily_log`), and conversation findings extracted by `MemoryMiddleware` (`domain='finding'`).

**Hot memory** — chunks stored under `domain='hot'` are *always-on*: `KnowledgeMiddleware` injects them into context every turn (vs. retrieved-on-relevance), re-read each turn so a freshly-added hot fact is seen immediately. Set one with `memory_ingest(content, domain="hot")` for facts the agent should never forget (operator preferences, standing constraints).

## `skills`

Human-authored skills in the AgentSkills [`SKILL.md`](../guides/skills.md) format — a folder with YAML frontmatter (`name` + `description`) and a markdown body. Loaded from disk into an FTS5 index on boot and retrieved + injected (`<learned_skills>`) at inference by `KnowledgeMiddleware`.

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Load `SKILL.md` skills and activate skill retrieval. |
| `db_path` | `/sandbox/skills.db` | FTS5 index path. Falls back to `~/.protoagent/skills.db` when the configured path isn't writable. |
| `top_k` | `5` | Max skills injected per turn (ranked by BM25 relevance to the message). |
| `dir` | `""` | Optional override for the *writable* skills root. Default: `<config-dir>/skills` (where `<config-dir>` honors `PROTOAGENT_CONFIG_DIR`). |

Skills load from two roots — bundled (`config/skills/`, shipped) and writable (`<config-dir>/skills/`, your drop-ins); live skills override bundled ones by `name`. `GET /api/runtime/status` reports `skills.count`. See the [Skills guide](../guides/skills.md) for authoring.

## `mcp`

Connect external [Model Context Protocol](../guides/mcp.md) servers; their tools become agent tools (namespaced `<server>__<tool>`). Off by default — adding a server is the opt-in. Built on `langchain-mcp-adapters`.

| Key | Default | What |
|---|---|---|
| `enabled` | `false` | Connect the configured servers and expose their tools. |
| `timeout_seconds` | `20` | Per-server discovery timeout. A slow/unreachable server is skipped, never fatal. |
| `denylist` | `[]` | Namespaced tool names to drop (e.g. `filesystem__write_file`). |
| `servers` | `[]` | List of `{name, transport, …}`. `stdio` → `command`/`args`/`env`/`cwd`; `streamable_http`/`sse` → `url`/`headers`. Per-server: `enabled: false` skips connecting it (lazy); `tools: {include: [...], exclude: [...]}` filters which of its tools bind. |

Per-server `tools.include` is an **allowlist** (only those tools bind) — the fix for a server with a large catalog flooding context; `exclude` drops from the remainder (`include` wins on conflict). The global `denylist` is the cross-server hard block. Both match the bare or namespaced tool name. See [ADR 0005](../adr/0005-tool-pollution-and-progressive-disclosure.md) on tool pollution.

Servers are discovered at startup/reload. `GET /api/runtime/status` reports `mcp.servers` and `mcp.tool_count`. See the [MCP guide](../guides/mcp.md) and `examples/mcp/echo_server.py`.

## `checkpoint`

The conversation-history checkpointer (durable chat memory across restarts) and its pruning/harvest knobs.

```yaml
checkpoint:
  db_path: /sandbox/checkpoints.db   # blank = in-memory (history lost on restart)
  keep_per_thread: 5
  max_age_days: 30
  prune_interval_hours: 6
  harvest_enabled: true
```

| Key | Default | What |
|---|---|---|
| `db_path` | `/sandbox/checkpoints.db` | SQLite path (`/sandbox`→`~/.protoagent` fallback, instance-scoped). Blank → in-memory (chat history doesn't survive a restart). |
| `keep_per_thread` | `5` | How many checkpoints to retain per conversation thread. |
| `max_age_days` | `30` | Drop checkpoints older than this. |
| `prune_interval_hours` | `6` | How often the background pruner runs. |
| `harvest_enabled` | `true` | On thread retire, harvest its history into the knowledge store before purging. |

## `workflows`

Declarative multi-step recipes over subagents ([ADR 0002](../adr/0002-reusable-subagent-workflows.md)) — the `run_workflow` / `save_workflow` tools.

```yaml
workflows:
  enabled: true
  dir: /sandbox/workflows   # writable recipe root
```

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | Expose `run_workflow` / `save_workflow` and load `*.yaml` recipes. |
| `dir` | `/sandbox/workflows` | Writable recipe root (`/sandbox`→`~/.protoagent` fallback). Bundled recipes also load from `workflows/`. |

## `plugins`

Drop-in [plugins](../guides/plugins.md) (manifest + `register()`) that contribute tools, bundled skills, FastAPI routes, background surfaces, subagents, and managed MCP servers (ADR 0018/0019). They run **in-process** with the agent's privileges, so a third-party plugin is **disabled by default** — only enable plugins you trust. (First-party bundled plugins like `discord`/`google` ship `enabled: true` in their own manifest.)

| Key | Default | What |
|---|---|---|
| `enabled` | `[]` | Plugin `id`s to load. A plugin also loads if its own manifest has `enabled: true`. |
| `disabled` | `[]` | Plugin `id`s to force **OFF** even when their manifest says `enabled: true` — the way a fork drops a bundled first-party plugin (e.g. `discord`, `google`) without deleting its directory or editing core. |
| `dir` | `""` | Override the writable plugins root (default `<config-dir>/plugins`). |

Plugins load from two roots — bundled (`plugins/`, e.g. `hello`, `discord`, `google`) and writable (`<config-dir>/plugins/`); live overrides bundled by `id`. Plugin tools that shadow a core/MCP tool are skipped. `GET /api/runtime/status` reports `plugins[]` (`id`, `enabled`, `loaded`, `tools`, `skills`, routes/surfaces/subagents counts). See the [Plugins guide](../guides/plugins.md).

### Plugin-declared config sections (ADR 0019)

A plugin can **claim a top-level config section** and declare its keys/secrets/Settings in its manifest (`config_section` / `config` defaults / `secrets` / `settings`). The section is resolved (manifest defaults ⊕ YAML ⊕ secrets overlay) into `config.plugin_config["<section>"]` and surfaced as a Settings group — with **no** edit to `config.py` / `config_io.py` / `settings_schema.py`. This is where the **Discord** and **Google** config now lives:

```yaml
discord:                 # claimed by plugins/discord/ — NOT a core config field
  enabled: false
  admin_ids: []
  # bot_token → secrets.yaml (plugin-declared secret)
google:                  # claimed by plugins/google/
  enabled: false
  client_id: ""
  tz: ""
  # client_secret → secrets.yaml
```

A plugin section colliding with a reserved built-in (`model`, `mcp`, `plugins`, …) is ignored. Plugin secrets (e.g. `discord.bot_token`, `google.client_secret`) route to `secrets.yaml` dynamically — see **Secrets** above.

## Scheduler

Scheduler **enable/disable** is YAML-controlled (`middleware.scheduler` above) so the drawer can flip it without a restart. Backend **selection and runtime knobs** (which backend, where to write the sqlite, where to publish, etc.) are env-driven so the same container image can run under either backend without a rebuild. See [Schedule future work](/guides/scheduler) for the full guide.

| Env var | Default | What |
|---|---|---|
| `WORKSTACEAN_API_BASE` | unset | When set together with `WORKSTACEAN_API_KEY`, swaps the bundled local scheduler for the `WorkstaceanScheduler` HTTP adapter. |
| `WORKSTACEAN_API_KEY` | unset | Auth token sent as `X-API-Key` to Workstacean's `/publish`. |
| `WORKSTACEAN_TOPIC_PREFIX` | `cron.<agent_name>` | Override the bus topic the adapter fires on, when your Workstacean install uses a different convention. |
| `SCHEDULER_DB_DIR` | `/sandbox/scheduler` | Local backend: parent directory for `<agent_name>/jobs.db`. Falls back to `~/.protoagent/scheduler/<agent_name>/jobs.db` when unwritable. |
| `SCHEDULER_INVOKE_URL` | `http://127.0.0.1:<active_port>` | Local backend: where to POST `message/send` when a job fires. Override only if the agent's A2A endpoint isn't on localhost. |
| `SCHEDULER_DISABLED` | unset | Runtime escape hatch — set to `1` / `true` to drop the scheduler tools entirely without editing YAML. `middleware.scheduler: false` is the canonical opt-out. |
