# The `protoagent` command

Install, run, and manage an agent from the terminal with `protoagent`.
For an interactive fleet view, run `protoagent fleet`; for terminal chat, use an
A2A client such as [protoCLI](https://github.com/protoLabsAI/protoCLI).

## Install

```bash
uv tool install protolabs-agent   # or: pipx install protolabs-agent
protoagent --help                 # the command is `protoagent` (install name differs)
```

Upgrade with `uv tool upgrade protolabs-agent` (or `pipx upgrade protolabs-agent`).

In a source checkout, install the project into its own environment first. The
editable install exposes the same CLI while loading Python code from `src/`:

```bash
uv sync
uv run protoagent fleet --help
```

`uv run python -m server <subcommand>` also works. Both front doors route through
`src/server/cli.py::dispatch`.

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
| `protoagent fleet ls` · `up` · `down` · `new` · `rm` · `rename` · `remote add\|edit\|rm` · `pair` · `order` | Inspect, run and **manage** fleet **member** agents — **live from the running hub** when one answers, from this instance's `fleet.json` (through the `ops/` layer) otherwise (see below). `--json` on each. | [0042](../adr/0042-fleet-supervisor-unified-console.md) · [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent fleet friction` · `friction resolve <id>` | Read **every member's friction ledger** through the hub (`--kind`, `--severity major`, `--since 7d`, `--agent`, `--resolved`, `--raw`, `-v`, `--json`) — ranked worst-first, with signals several agents hit called out and unreadable members named. `resolve` stamps a signal fixed on the agents you name (`--agent`, or `--all-agents`; `--reason`, `--reopen`). Live hub only. | [friction log](friction-log.md#across-the-fleet-from-the-terminal) |
| `protoagent fleet --all` | The **hub tree**: every hub on this box (heartbeats, instance roots with a fleet, listeners by port) probed for its version and member counts, plus peers found on the network. `--offline` skips the scan and the probes. Not a member command: it never reads one hub's fleet. | [0042](../adr/0042-fleet-supervisor-unified-console.md) |
| `protoagent pair` | Print a **one-time code** another agent's hub claims to pair with the protoAgent running on this machine — the headless half of agent pairing (see below). `--device` pairs a **phone** instead: code, console URL and a terminal QR. `--json` for scripts. | [0113](../adr/0113-agent-pairing-for-remote-fleet-members.md) |
| `protoagent skills ls` · `promote <name>` | Inspect and curate the SKILL.md library. | [0041](../adr/0041-workspaces-and-tiered-stores.md) |
| `protoagent config explain` · `get` · `set key=value …` | Explain the config cascade; print `config.yaml`; write dotted keys (JSON-typed) to disk. | [0047](../adr/0047-layered-settings-cascade.md) · [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent knowledge ingest <url\|file>` | Fetch/extract a source and index it into this instance's knowledge base. | [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent operations` | List the operations on the shared ops layer — name, read/write, one-line summary. | [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent agent export [-o PATH] [--dry-run]` | Write this agent's **redacted snapshot** zip — the declarative recipe (SOUL, stripped config, plugin SHA pins, skills). Works on a **stopped** agent. | [0091](../adr/0091-agent-snapshot-portability.md) |
| `protoagent agent import <zip> [--name N] [--dry-run] [--yes]` | Stand up a **fresh agent** from a snapshot. Prints the plan (plugins it will install and run, capabilities it grants) and refuses to apply without `--yes`. | [0091](../adr/0091-agent-snapshot-portability.md) |
| `protoagent runtime use <rt>` · `list` | Select the agent runtime. **`native` (LangGraph) is the supported value**; the `acp:*` runtimes are [deprecated](/guides/acp-runtime) — hand coding jobs to an [`acp` delegate](/guides/coding-agents) instead. | [0033](../adr/0033-pluggable-agent-runtime-acp.md) |
| `protoagent hermes` | **Deprecated** ([#2633](https://github.com/protoLabsAI/protoAgent/issues/2633)) — the Hermes preset still works for existing installs but is no longer offered. Hand work to an external agent with [ACP delegates](delegates.md) instead. | [0033](../adr/0033-pluggable-agent-runtime-acp.md) |

#### The fleet deck: `protoagent fleet` with no arguments

Run `protoagent fleet` (or `protoagent top`) for the interactive terminal UI.
Use `protoagent fleet --all` to choose among hubs, or `--hub URL --token TOKEN`
to select one explicitly. `q` closes the deck while members keep running.

The [fleet-deck guide](./fleet-deck.md) owns the keyboard controls, conversations,
approvals, member management, offline behavior, and desktop binary instructions.
For scripts, use `protoagent fleet ls --json` or `protoagent fleet --all --json`;
these commands do not load the terminal UI.

#### `fleet` talks to the running hub

`fleet ls`, `up`, and `down` use a running hub's API when available. The hub owns
member processes and supplies the same live roster as the console.

How a hub is found, in order: this instance's `server.pid` (from `protoagent up`), the
`.instances/<pid>.json` heartbeats every server writes under its box root — scanned
across every box root this machine uses, including the desktop app's — then `:7870`.
Among heartbeats, a box root's own instance (the desktop app, a default `~/.protoagent`)
outranks a **named** instance beneath one (`~/.protoagent/dev`, the `scripts/dev.sh`
sandbox), so the sandbox never shadows the real fleet. When several hubs are running the
CLI says so on stderr; `fleet --all` lists them and `--hub URL` picks one.
How it is opened: `--token` / `PROTOAGENT_HUB_TOKEN`, then the hub's own fleet service
token (`<instance root>/workspaces/.fleet-token`, ADR [0089](../adr/0089-intra-instance-trust-boundary.md)),
then `A2A_AUTH_TOKEN`, then no credential. Tokens are never printed.

```bash
protoagent fleet ls                       # live · http://127.0.0.1:7870 · protoagent v0.165.0 · via heartbeat
protoagent fleet ls --json | jq '.agents[] | select(.running) | .name'
protoagent fleet up protoEngineer         # POST /api/fleet/protoEngineer/start — the hub owns the process
protoagent fleet down                     # POST /api/fleet/down
protoagent fleet new scout --archetype pm       # from the hub's catalog (live); --bundle <git-url> works offline too
protoagent fleet new blank --no-start --no-inherit
protoagent fleet rename scout scout-prime        # display name only (letters, digits, - and _); the id and slug stay
protoagent fleet rm scout --purge               # asks you to type the name; --yes off a terminal
protoagent fleet remote add ava https://ava.tail:7870 --bearer-stdin < token.txt
protoagent fleet remote edit ava --url https://ava2.tail:7870 --clear-bearer
protoagent fleet pair http://100.64.0.5:7870 ABCDE-12345   # claim a code minted on the remote; the hub stores the token (ADR 0113)
protoagent fleet pair http://100.64.0.5:7870                # no code in argv: prompted (no echo), or --code-stdin from a pipe
protoagent fleet pair http://192.168.1.20:7870 --insecure-http   # plain http off loopback/tailnet: refused unless you opt in (ADR 0113 D10)
protoagent fleet order protoagent scout-1a2b r-ava   # every member id, in the order wanted
protoagent fleet --all                          # every hub on this box (and peers), probed; --json for scripts
protoagent fleet ls --hub https://ava.tail:7870 --token "$TOKEN"   # a hub elsewhere (an explicit --hub that fails is an error, not a fallback)
protoagent fleet ls --hub http://100.119.239.8:7870 --token "$TOKEN" --insecure-http   # a tailnet peer: http, but encrypted underneath
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
stray URL can never harvest local credentials — and a credential is **never sent in
cleartext off-box**: a non-loopback `http://` hub is refused unless you pass
`--insecure-http` for a link you know is encrypted underneath (a tailnet). Redirects are
never followed. A fleet *member* is refused as a hub even when named explicitly: it is a
fleet of itself, and lifecycle belongs to its hub. `--json` emits per-member result rows of
one shape (`{name, ok, …}`) plus `mode` and `hub`.

#### Pairing another agent with this one: `protoagent pair`

A hub adds a remote protoAgent by **pairing** with it rather than being handed its token
([ADR 0113](../adr/0113-agent-pairing-for-remote-fleet-members.md)). The remote shows a
one-time code, and the hub claims it. The claim mints a token for that hub alone, which the
remote lists under **Settings ▸ Devices** and can revoke without touching anything else.

On a machine with a console, the code comes from Settings ▸ Devices ▸ *Pair an agent*. On a
headless box (docker, a server), run `protoagent pair` beside the running instance. It finds
the instance the same way `fleet` finds a hub, and prints the code with a claim command for
each address the instance is reachable on:

```bash
$ protoagent pair
Pairing code for ava:  K7QM2-XPA4F   (expires in 4:59)

On the hub, enter it under Settings ▸ Fleet ▸ Discover ▸ Pair…, or run:
  protoagent fleet pair http://100.64.1.2:7870 K7QM2-XPA4F   (tailnet)
  protoagent fleet pair http://192.168.1.20:7870 K7QM2-XPA4F   (lan)
```

The code is ten characters, case-insensitive (dashes optional), works **once**, and expires
after five minutes. Five wrong guesses at the instance cancel every pending code. An
instance bound to loopback can't be paired, because nothing else can reach it:
`protoagent pair` says so, lists the addresses it could use, and points at the fix. The fix
is a reachable bind *with* an auth token (`--host 0.0.0.0`, or *Allow devices on my network*
in Settings ▸ Devices), never an open instance. A fleet member listens on loopback behind
its hub, so pair the hub instead.

#### Pairing a phone from the terminal: `protoagent pair --device`

Pairs a phone or tablet with the protoAgent running on this machine, for a box with no
screen to open **Settings ▸ Devices** on
([ADR 0087](../adr/0087-device-pairing-and-per-device-tokens.md)). It authenticates to the
instance exactly like `protoagent pair`, starts a phone pairing, and prints a terminal QR,
the code, and the console URL for each reachable address:

```text
$ protoagent pair --device
Scan with the phone's camera:
█████████████████████████████████████
██ ▄▄▄▄▄ █ ▀▀ ▄ ██▄▄▀▄█▀▀ ▄█ ▄▄▄▄▄ ██
   … (QR continues) …

Pairing code:  K7QM2-XPA4F   (expires in 4:59)

Or on the phone, open this agent and tap "Have a pairing code?", then type the code:
  http://100.64.1.2:7870/app/   (any network the phone is on, if it runs Tailscale)
  http://192.168.1.20:7870/app/   (same Wi-Fi only)

The code works once. The phone shows up under Settings ▸ Devices, where you can revoke it.
```

The QR encodes the first (tailnet-first) address with the code in its `#pair=` fragment.
The code follows the same rules as an agent code: ten characters, case-insensitive, works
once, expires after five minutes, and five wrong tries cancel every pending code.

| Flag | Effect |
| --- | --- |
| `--no-qr` | Print only the code and URLs. |
| `--light` | Draw the QR for a light-background terminal. The default suits a dark one. |
| `--json` | Print the instance's raw answer (`code`, `expires_at`, `hosts[].base_url`, `hosts[].url`). |
| `--url`, `--token` | Pair a specific instance, as for `protoagent pair`. |

On a loopback-bound instance it exits 1 and explains the fix, the same as `protoagent pair`.
On a `--ui none` instance it exits 1 with *Restart it with --ui console (the default) to
pair a phone*: there is no console for the phone to open, so the instance makes no code.
See [Pair a phone by typing the code](./pairing.md#pair-a-phone-by-typing-the-code).

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

A snapshot creates a fresh agent from its persona, redacted configuration, plugin
pins, MCP definitions, and skills. It does not restore chats or runtime state.
Review `REVIEW.md` and the exported files before sharing: recognized secrets are
removed, but private prose and unrecognized credentials can remain.

For the app controls, see [Export or copy an agent](/guides/agent-snapshots).
Use [Back up and restore data](/guides/backup-and-restore) for full recovery.

### Importing an agent

```bash
protoagent agent import vera-snapshot.zip --dry-run        # the plan; changes nothing
protoagent agent import vera-snapshot.zip --name vera-2 --yes \
  --secret providers.gateway=sk-…
```

Import installs and enables the plugin code named in its plan. Read the repository
URLs, granted capabilities, and required credentials before applying with `--yes`.
Supply credentials to the new agent through its Settings, or use repeatable
`--secret NAME=VALUE` arguments; command-line secrets can appear in shell history.

See [Snapshot reference](/reference/agent-snapshots) for contents, knowledge
exclusions, credential names, and model-connection inheritance.

## Shared operations {#roadmap}

`protoagent operations` lists the shared operations available in this build.
See the [operator API reference](/reference/operator-api) and
[operator MCP guide](/guides/mcp) for the other interfaces.
