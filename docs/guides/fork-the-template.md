# Fork the template

Same checklist as `TEMPLATE.md` in the repo, kept in sync. Use this when you've forked before and don't need the tutorial walkthrough — just the list.

## 0. Pick a name

One short slug. Ends up in:

- `AGENT_NAME` env var
- Prometheus metric prefix (`<name>_llm_calls_total`, etc.)
- Langfuse trace tag
- Docker image label + GHCR path
- Release pipeline repo guards

## 1. Rename

```bash
git grep -li protoagent | xargs sed -i 's/protoagent/<your-name>/g'
git grep -li protoAgent | xargs sed -i 's/protoAgent/<YourName>/g'
```

Review the diff. Key hits: `Dockerfile` (`/opt/protoagent`), `IMAGE_NAME` in the workflow files, `chat_ui.py` branding.

## 2. Un-freeze release pipeline

Change the `github.repository == 'protoLabsAI/protoAgent'` guard in:

- `.github/workflows/prepare-release.yml`
- `.github/workflows/release.yml`

Until this lands, releases won't fire. Intentional, not a bug.

## 3. Rewrite identity

| File | What goes in it |
|---|---|
| `config/SOUL.md` | Persona doc loaded into workspace at session start |
| `graph/prompts.py::build_system_prompt` | Lead agent system prompt |
| `graph/prompts.py::build_subagent_prompt` | Per-subagent delegation prompt |
| `server.py::_build_agent_card` | `name`, `description`, `skills`, declared extensions |

Keep the `<scratch_pad>` / `<output>` protocol block in `prompts.py` — the A2A handler's output extraction depends on it.

## 4. Replace the starter tools

`tools/lg_tools.py` ships with `current_time`, `calculator`, `web_search`, `fetch_url`. Keep what you want, drop the rest, add your own. Update `get_all_tools()` at the bottom of the file.

See the [starter tools reference](/reference/starter-tools) for the shapes of the shipped ones.

## 5. Configure subagents (optional)

`graph/subagents/config.py` ships with one `worker`. Either:

- Add more by registering `SubagentConfig` instances in `SUBAGENT_REGISTRY` and matching fields in `graph/config.py::LangGraphConfig`, or
- Call `create_agent_graph(config, include_subagents=False)` in `server.py::_init_langgraph_agent()` to skip subagents entirely.

See [Configure subagents](/guides/subagents) for the full pattern.

## 6. Point at a model

Edit `config/langgraph-config.yaml::model.name`. Two options:

1. **Gateway alias** — register `protolabs/<your-name>` in your LiteLLM gateway, set `name: protolabs/<your-name>`. Swapping models becomes a gateway edit.
2. **Direct model** — set `name: openai/gpt-4o` or `anthropic/claude-opus-4-6` and let the gateway route through directly.

Option 1 is preferred.

## 7. Deploy

See [Deploy via GHCR](/guides/deploy). The Dockerfile uses a single `COPY . /opt/protoagent/` so new files don't need Dockerfile updates.

## 8. Delete `TEMPLATE.md`

Once the checklist is done, delete it and rewrite `README.md` to describe your specific agent.
