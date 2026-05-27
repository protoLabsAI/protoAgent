# Configuration

`config/langgraph-config.yaml` is the canonical runtime config. Loaded at server boot by `graph/config.py::LangGraphConfig.from_yaml()`. All fields have defaults; the YAML only needs to override what's changing.

## Full example

```yaml
model:
  provider: openai
  name: protolabs/agent
  api_base: http://gateway:4000/v1
  api_key: ""
  temperature: 0.2
  max_tokens: 4096
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
| `name` | `protolabs/agent` | Gateway alias or direct model name. |
| `api_base` | `http://gateway:4000/v1` | OpenAI-compatible endpoint. |
| `api_key` | `""` | Falls back to the `OPENAI_API_KEY` env var. |
| `temperature` | `0.2` | Sampling temperature. |
| `max_tokens` | `4096` | Per-call output cap. |
| `max_iterations` | `50` | Upper bound on tool-call loops per task. |
| `top_p` | _(unset)_ | Nucleus sampling. Standard OpenAI param; sent only when set. |
| `presence_penalty` | _(unset)_ | Standard OpenAI param; sent only when set. |
| `top_k` | `-1` | Top-k sampling. Rides `extra_body` (vLLM-style gateways). `-1`/negative = gateway default. |
| `repetition_penalty` | _(unset)_ | Rides `extra_body`; sent only when set. |
| `chat_template_kwargs` | _(unset)_ | Dict passed via `extra_body` to the vLLM renderer, e.g. `{preserve_thinking: true}` to keep historical `<think>`/`<scratch_pad>` blocks across turns. |

All sampling params are optional — omit to use the gateway / model-card defaults. `temperature`, `max_tokens`, `top_p`, and `presence_penalty` are standard OpenAI fields; `top_k`, `repetition_penalty`, and `chat_template_kwargs` are sent via `extra_body` for vLLM-compatible gateways.

## `subagents`

One entry per subagent name. Each entry matches a `SubagentConfig` in `graph/subagents/config.py` and a `SubagentDef` field in `LangGraphConfig`.

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | If false, the subagent is still registered but dispatches return "disabled" errors. |
| `tools` | `[]` | Allowlist. Tool names not listed here are invisible to this subagent. |
| `max_turns` | `30` | Recursion cap. |

Adding a new subagent name to the YAML requires matching entries in `graph/subagents/config.py::SUBAGENT_REGISTRY`, `graph/config.py::LangGraphConfig`, and the `from_yaml()` loop. See [Configure subagents](/guides/subagents).

## `middleware`

| Key | Default | What |
|---|---|---|
| `knowledge` | `true` | Inject retrieved knowledge into state before LLM calls. Backed by the bundled `KnowledgeStore` (sqlite + FTS5). Set `false` for a stateless agent. |
| `audit` | `true` | Append every tool call to `/sandbox/audit/audit.jsonl`. |
| `memory` | `true` | Persist a session summary on terminal turn and asynchronously index conversation findings under `domain='finding'`. |
| `scheduler` | `true` | Wire the bundled scheduler backend (local sqlite, or `WorkstaceanScheduler` when env vars are set). Drops the `schedule_task` / `list_schedules` / `cancel_schedule` tools from the agent loop when `false`. Has the same effect as `SCHEDULER_DISABLED=1` — but `middleware.scheduler: false` is the canonical opt-out (drawer/wizard editable, survives restarts), while the env var is a runtime escape hatch for fleet operators who can't edit YAML in the moment. |

## `knowledge`

Only read when `middleware.knowledge` is `true`.

| Key | Default | What |
|---|---|---|
| `db_path` | `/sandbox/knowledge/agent.db` | SQLite file path. Falls back to `~/.protoagent/knowledge/agent.db` automatically when the configured path isn't writable (e.g. running locally without `/sandbox`). Override at runtime with `KNOWLEDGE_DB_PATH`. |
| `embed_model` | `nomic-embed-text` | Reserved for forks that bolt embeddings on top of the FTS5 baseline. The bundled store ignores it. |
| `top_k` | `5` | Results per query fed into state. |

The bundled store is sqlite + FTS5 (with an automatic LIKE fallback when FTS5 isn't available). One `chunks` table; the `domain` column distinguishes operator-set notes (`memory_ingest`), daily-log entries (`daily_log`), and conversation findings extracted by `MemoryMiddleware` (`domain='finding'`).

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
