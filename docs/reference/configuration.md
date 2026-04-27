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
  worker:
    enabled: true
    tools:
      - current_time
      - calculator
      - web_search
      - fetch_url
      - memory_ingest
      - memory_recall
      - memory_list
      - memory_stats
      - daily_log
    max_turns: 20

middleware:
  knowledge: true
  audit: true
  memory: true

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

## `knowledge`

Only read when `middleware.knowledge` is `true`.

| Key | Default | What |
|---|---|---|
| `db_path` | `/sandbox/knowledge/agent.db` | SQLite file path. Falls back to `~/.protoagent/knowledge/agent.db` automatically when the configured path isn't writable (e.g. running locally without `/sandbox`). Override at runtime with `KNOWLEDGE_DB_PATH`. |
| `embed_model` | `nomic-embed-text` | Reserved for forks that bolt embeddings on top of the FTS5 baseline. The bundled store ignores it. |
| `top_k` | `5` | Results per query fed into state. |

The bundled store is sqlite + FTS5 (with an automatic LIKE fallback when FTS5 isn't available). One `chunks` table; the `domain` column distinguishes operator-set notes (`memory_ingest`), daily-log entries (`daily_log`), and conversation findings extracted by `MemoryMiddleware` (`domain='finding'`).
