# Fork the template

Same checklist as `TEMPLATE.md` in the repo, kept in sync. Use this when you've forked before and don't need the tutorial walkthrough — just the list.

> **Re-sync is the design goal.** Everything below customizes via **config,
> SOUL.md, plugins, and env** — *not* by editing core files. The fewer tracked
> files you touch, the cleaner `git merge upstream/main` (or a cherry-pick)
> stays. `CHANGELOG.md` is set to `merge=union` (`.gitattributes`), so your
> entries and upstream's coexist instead of conflicting.

## 0. Name + identity (config, not a rename)

Set your agent's **user-facing** name in **config** — it flows to the console
brand, window/tab title, agent card, and system prompt:

- `identity.name` in `config/langgraph-config.yaml` (or the setup wizard), and
- `config/SOUL.md` for persona — it's loaded into the system prompt, so you don't
  edit `graph/prompts.py`. (Keep the `<scratch_pad>`/`<output>` protocol block if
  you ever do touch prompts — the A2A handler's output extraction depends on it.)

**Do NOT `sed` the internal `protoagent` identifier.** It's the stable template
name for logger namespaces, the `~/.protoagent` data dir, `PROTOAGENT_*` env
vars, and the plugin namespace — all internal, never user-facing. Renaming it
rewrites ~120 files and makes *every* upstream merge conflict, for zero
functional gain. Leave it.

The few places that genuinely want your slug are **env-driven**, no file edit:

- `AGENT_NAME` env var (Prometheus prefix, Langfuse tag, A2A `<NAME>_API_KEY`).
- Docker image label / GHCR path — set in *your* deploy, not the template.

## 1. Enable the release pipeline (no workflow edit)

Set the **`RELEASE_ENABLED` repo variable** to `true`:

```bash
gh variable set RELEASE_ENABLED --body true
```

The release workflows gate on it, so you enable releases without touching
`prepare-release.yml` / `release.yml` — and upstream changes to those files
re-sync cleanly. Until the variable is set, releases won't fire (intentional).

## 2. Tools — keep / drop / add (config + plugins, no core edit)

The starter tools ship by default: `current_time`, `calculator`, `web_search`,
`fetch_url` (keyless general) plus the memory, scheduler, notes, GitHub, and
beads tools.

- **Drop** the ones you don't want via config — list them under `tools.disabled`
  in `config/langgraph-config.yaml` (live-reloadable). No `get_all_tools()` edit.
- **Add** your own as a **plugin** (`plugins/<id>/` with a `register(registry)`),
  so they're discovered without touching core. See [Plugins](/guides/plugins).

(Editing `tools/lg_tools.py::get_all_tools()` directly still works, but it's a
core edit that conflicts on every upstream re-sync — prefer config + plugins.)

Integrations are *plugins*. The bundled ones (e.g. `plugins/telegram`, `plugins/github`)
turn off with `plugins: { disabled: [telegram] }` — no directory delete, no core edit.
Integrations like **Discord**, **Google**, and **Slack** install as **external** plugins
from their own repos (browse + install in Settings ▸ Plugins ▸ Discover).

See the [starter tools reference](/reference/starter-tools) for the shapes of the shipped ones.

## 3. Configure subagents (optional)

`graph/subagents/config.py` ships with one `researcher`. Either:

- Add more by registering `SubagentConfig` instances in `SUBAGENT_REGISTRY` and matching fields in `graph/config.py::LangGraphConfig`, or
- Call `create_agent_graph(config, include_subagents=False)` in `server/agent_init.py::_init_langgraph_agent()` to skip subagents entirely.

See [Configure subagents](/guides/subagents) for the full pattern.

## 4. Point at a model

Edit `config/langgraph-config.yaml::model.name`. Two options:

1. **Gateway alias** — register `protolabs/<your-name>` in your LiteLLM gateway, set `name: protolabs/<your-name>`. Swapping models becomes a gateway edit.
2. **Direct model** — set `name: openai/gpt-4o` or `anthropic/claude-opus-4-8` and let the gateway route through directly.

Option 1 is preferred.

## 5. Deploy

See [Deploy via GHCR](/guides/deploy). The Dockerfile uses a single `COPY . /opt/protoagent/` so new files don't need Dockerfile updates.

## 6. Delete `TEMPLATE.md`

Once the checklist is done, delete it and rewrite `README.md` to describe your specific agent.
