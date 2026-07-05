# The `protoagent` command

`protoagent` is the terminal control plane for a protoAgent runtime — install,
run, and manage an instance without touching the console. It's the discoverable
front door that replaces the bare `python -m server <subcommand>` invocation
(ADR 0075 — added in a follow-up).

> Chatting with an agent is a separate job — that's what [`proto`](https://github.com/protoLabsAI/protoCLI)
> (the A2A terminal client) is for. `protoagent` runs and manages the runtime;
> `proto` talks to it. They meet at the wire (A2A / ACP), not in one binary.

## Install

```bash
uv tool install protoagent        # or: pipx install protoagent
protoagent --help
```

In a source checkout you can also run it through uv without installing:

```bash
uv run protoagent --help
```

`python -m server <subcommand>` keeps working — both front doors route through the
same dispatcher (`server/cli.py::dispatch`), so they can never drift.

## Commands

```
protoagent --help
```

### Lifecycle

| Command | What it does |
|---|---|
| `protoagent serve [--port N]` | Run the server in the **foreground** (identical to `python -m server`). |
| `protoagent up [--port N] [--host H]` | Start the server **detached** (background), boot-watch the port, and record a pidfile at the instance root. |
| `protoagent down` | Stop the server started by `up` (SIGTERM, then SIGKILL after ~8s). Refuses to kill a server it didn't launch. |
| `protoagent status` | Report whether this instance's server is running — port, pid, version. Exit code `0` = running, `3` = stopped. |
| `protoagent setup` | Complete headless setup for the live config (ADR [0010](../adr/0010-headless-setup-and-ui-tiers.md)) — validates the model endpoint/key and marks setup complete. |

`up` / `down` / `status` act on **this instance** (scoped by `PROTOAGENT_INSTANCE`
/ `PROTOAGENT_HOME`). To manage the multi-agent *fleet*, use `protoagent fleet`.

### Management

Each forwards to the same core the console REST API calls, and acts on disk/DBs
then exits:

| Command | What it does | ADR |
|---|---|---|
| `protoagent plugin install <git-url>` · `list` · `update` · `uninstall` · `sync` | Manage drop-in plugins (pinned in `plugins.lock`). | [0027](../adr/0027-install-plugins-from-git-url.md) |
| `protoagent workspace new` · `ls` · `run` · `rm` | Named, isolated agents on one host. | [0041](../adr/0041-workspaces-and-tiered-stores.md) |
| `protoagent fleet up` · `down` · `ls` | Run fleet **member** agents as background processes. | [0042](../adr/0042-fleet-supervisor-unified-console.md) |
| `protoagent skills ls` · `promote <name>` | Inspect and curate the SKILL.md library. | [0041](../adr/0041-workspaces-and-tiered-stores.md) |
| `protoagent config explain` | Print this instance's id, both roots, every resolved path, and the config cascade with provenance. | [0047](../adr/0047-layered-settings-cascade.md) |

## Examples

```bash
# Stand up an instance and check it
protoagent up --port 7870
protoagent status
protoagent config explain

# Install a plugin, then reload isn't needed for a fresh boot
protoagent plugin install https://github.com/protoLabsAI/careercoach-plugin

# Stop it
protoagent down
```

## Roadmap

Later slices of ADR 0075 add `protoagent model` (point at a local Ollama / HF /
LM Studio / vLLM endpoint in one command) and a shared operation layer so every
verb here has a matching MCP tool and REST endpoint. See the ADR for the plan.
