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
uv tool install protolabs-agent   # or: pipx install protolabs-agent
protoagent --help                 # the command is `protoagent` (install name differs)
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
| `protoagent fleet ls` · `up` · `down` | Inspect and run fleet **member** agents — **live from the running hub** when one answers, from this instance's `fleet.json` otherwise (see below). `--json` on each. | [0042](../adr/0042-fleet-supervisor-unified-console.md) · [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent skills ls` · `promote <name>` | Inspect and curate the SKILL.md library. | [0041](../adr/0041-workspaces-and-tiered-stores.md) |
| `protoagent config explain` · `get` · `set key=value …` | Explain the config cascade; print `config.yaml`; write dotted keys (JSON-typed) to disk. | [0047](../adr/0047-layered-settings-cascade.md) · [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent knowledge ingest <url\|file>` | Fetch/extract a source and index it into this instance's knowledge base. | [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent operations` | List the operations on the shared ops layer — name, read/write, one-line summary. | [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent agent export [-o PATH] [--dry-run]` | Write this agent's **secret-free snapshot** zip — the declarative recipe (SOUL, stripped config, plugin SHA pins, skills). Works on a **stopped** agent. | [0091](../adr/0091-agent-snapshot-portability.md) |
| `protoagent agent import <zip> [--name N] [--dry-run] [--yes]` | Stand up a **fresh agent** from a snapshot. Prints the plan (plugins it will install and run, capabilities it grants) and refuses to apply without `--yes`. | [0091](../adr/0091-agent-snapshot-portability.md) |
| `protoagent runtime use <rt>` · `list` | Select the agent runtime. **`native` (LangGraph) is the supported value**; the `acp:*` runtimes are [deprecated](/guides/acp-runtime) — hand coding jobs to an [`acp` delegate](/guides/coding-agents) instead. | [0033](../adr/0033-pluggable-agent-runtime-acp.md) |
| `protoagent hermes` | **Deprecated** ([#2633](https://github.com/protoLabsAI/protoAgent/issues/2633)) — the Hermes preset still works for existing installs but is no longer offered. Hand work to an external agent with [ACP delegates](delegates.md) instead. | [0033](../adr/0033-pluggable-agent-runtime-acp.md) |

#### `fleet` talks to the running hub

`fleet ls` / `up` / `down` look for a **running hub** before they read anything from
disk, because the hub is the only source of live truth about the fleet: its
`GET /api/fleet` is what the console shows, and its control plane is what owns the
member processes. A shell that read `fleet.json` from its own instance root used to
report a fleet of one beside the desktop app's hub (which lives under a different
`PROTOAGENT_HOME`) — and called the CLI's own pid a running server.

How a hub is found, in order: this instance's `server.pid` (from `protoagent up`), the
`.instances/<pid>.json` heartbeats every server writes under its box root — scanned
across every box root this machine uses, including the desktop app's — then `:7870`.
How it is opened: `--token` / `PROTOAGENT_HUB_TOKEN`, then the hub's own fleet service
token (`<instance root>/workspaces/.fleet-token`, ADR [0089](../adr/0089-intra-instance-trust-boundary.md)),
then `A2A_AUTH_TOKEN`, then no credential. Tokens are never printed.

```bash
protoagent fleet ls                       # live · http://127.0.0.1:7870 · protoagent v0.165.0 · via heartbeat
protoagent fleet ls --json | jq '.agents[] | select(.running) | .name'
protoagent fleet up protoEngineer         # POST /api/fleet/protoEngineer/start — the hub owns the process
protoagent fleet down                     # POST /api/fleet/down
protoagent fleet ls --hub ava.tail:7870 --token "$TOKEN"   # a hub elsewhere (an explicit --hub that fails is an error, not a fallback)
protoagent fleet ls --offline             # this instance's fleet.json, no probe
```

When **nothing** answers the output is badged `offline · reading <fleet.json>` and `up` /
`down` act through the supervisor on disk — the right thing only when nothing is
running. A hub that answered but could not be opened (rejected credential, timeout,
5xx, or only a fleet *member* answering) is an **error, not a fallback**: driving
processes from disk beside a running hub is exactly the two-hubs bug. A member's `401`
is reported as that member's credential problem, never as the hub's.

This box's fleet service tokens and `A2A_AUTH_TOKEN` are sent to **loopback hubs only**.
A `--hub` on another host gets `--token` / `PROTOAGENT_HUB_TOKEN` and nothing else, so a
stray URL can never harvest local credentials. `--json` emits per-member result rows of
one shape (`{name, ok, …}`) plus `mode` and `hub`.

### Point at a local model

`protoagent model` points protoAgent at any OpenAI-compatible endpoint — the gateway
is the default, not a lock-in, so a local Ollama / LM Studio / llama.cpp / vLLM server
is one line:

```bash
protoagent model discover                                   # probe :11434 / :1234 / :8080
protoagent model use --base-url http://127.0.0.1:8080/v1 --model qwen2.5
protoagent up
```

`model use` writes the endpoint + model to your live config (a local endpoint ignores
the key; a placeholder is set so the client constructs — use `--key` or `secrets.yaml`
for a real gateway key). This one-liner is also the copy-paste target for HuggingFace's
"Use this model" local-app snippet — a HF model card hands the model id straight to it.

**Pick a tool-calling model.** protoAgent drives tools on every turn, so the local model
must support tool/function calling (e.g. `llama3.2`, `qwen2.5`) — point it at one that
doesn't and the turn fails at the endpoint with `does not support tools`.

## Examples

```bash
# Stand up an instance and check it
protoagent up --port 7870
protoagent status
protoagent config explain

# Point at a local LLM
protoagent model use --base-url http://127.0.0.1:11434/v1 --model llama3.2

# Install a plugin, then reload isn't needed for a fresh boot
protoagent plugin install https://github.com/protoLabsAI/careercoach-plugin

# Edit config headless, ingest a doc, list what operations exist
protoagent config set fleet.mdns.enabled=false
protoagent knowledge ingest https://example.com/post --domain research
protoagent operations

# Stop it
protoagent down
```

### Exporting an agent

```bash
protoagent agent export --dry-run     # review only: what is stripped, what the target must supply
protoagent agent export -o ~/snapshots/   # write the zip
```

The snapshot is a **recipe, not a backup**: SOUL, secret-stripped config, `plugins.lock`
SHA pins, MCP server definitions and `SKILL.md` dirs. No runtime history, no credentials,
no plugin code — importing yields a *fresh* agent, not a resumed one.

Credentials never travel. What the target must re-supply is listed by name in a
`required_secrets` inventory, and every zip carries a `REVIEW.md` spelling out what was
stripped and what still needs re-pointing. Two things it distinguishes, because the
response differs:

- **Credential-shaped text found in free text** (a token pasted into `SOUL.md` or a config
  field) — scrubbed from the artifact, but still in the *source* agent. Treat it as exposed
  and rotate it.
- **Machine-local paths** — scrubbed because they carry your username. Nothing to rotate;
  re-point them after import.

Redaction of free text is a safety net, not a guarantee — read the artifact before you
publish it.

The same export is in the console at **Settings ▸ Agent ▸ Snapshot**, which shows the review
first and downloads the zip on a second click.

### Importing an agent

```bash
protoagent agent import vera-snapshot.zip --dry-run        # the plan; changes nothing
protoagent agent import vera-snapshot.zip --name vera-2 --yes \
  --secret model.api_key=sk-…
```

**Importing runs code.** A snapshot names plugin repos, and applying it clones them and
enables them in-process — so `import` always prints its plan first (every URL, with
unfamiliar sources flagged, plus the capabilities the config grants) and refuses to apply
until you pass `--yes`. Read the plan; it is describing what is about to run on your machine.

The config applies **verbatim**, including capability settings like `filesystem.allow_run`
and `operator.allowed_dirs` — those are part of the agent's definition, so they're shown in
the plan rather than silently stripped.

The new agent arrives **incomplete** until its credentials are supplied: none travel in a
snapshot. Pass them with `--secret NAME=VALUE` (repeatable, written `0600` to the new agent
only), or set them afterwards in that agent's Settings ▸ Secrets. Only credentials the
*source* agent actually had are reported missing.

## Roadmap

Later slices of ADR 0075 add a shared operation layer so every verb here has a matching
MCP tool and REST endpoint. See the ADR for the plan.
