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
| `protoagent fleet ls` · `up` · `down` · `new` · `rm` · `rename` · `remote add\|edit\|rm` · `order` · `--all` | Inspect, run and **manage** fleet **member** agents — **live from the running hub** when one answers, from this instance's `fleet.json` (through the `ops/` layer) otherwise (see below). `--json` on each. | [0042](../adr/0042-fleet-supervisor-unified-console.md) · [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent skills ls` · `promote <name>` | Inspect and curate the SKILL.md library. | [0041](../adr/0041-workspaces-and-tiered-stores.md) |
| `protoagent config explain` · `get` · `set key=value …` | Explain the config cascade; print `config.yaml`; write dotted keys (JSON-typed) to disk. | [0047](../adr/0047-layered-settings-cascade.md) · [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent knowledge ingest <url\|file>` | Fetch/extract a source and index it into this instance's knowledge base. | [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent operations` | List the operations on the shared ops layer — name, read/write, one-line summary. | [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |
| `protoagent agent export [-o PATH] [--dry-run]` | Write this agent's **secret-free snapshot** zip — the declarative recipe (SOUL, stripped config, plugin SHA pins, skills). Works on a **stopped** agent. | [0091](../adr/0091-agent-snapshot-portability.md) |
| `protoagent agent import <zip> [--name N] [--dry-run] [--yes]` | Stand up a **fresh agent** from a snapshot. Prints the plan (plugins it will install and run, capabilities it grants) and refuses to apply without `--yes`. | [0091](../adr/0091-agent-snapshot-portability.md) |
| `protoagent runtime use <rt>` · `list` | Select the agent runtime. **`native` (LangGraph) is the supported value**; the `acp:*` runtimes are [deprecated](/guides/acp-runtime) — hand coding jobs to an [`acp` delegate](/guides/coding-agents) instead. | [0033](../adr/0033-pluggable-agent-runtime-acp.md) |
| `protoagent hermes` | **Deprecated** ([#2633](https://github.com/protoLabsAI/protoAgent/issues/2633)) — the Hermes preset still works for existing installs but is no longer offered. Hand work to an external agent with [ACP delegates](delegates.md) instead. | [0033](../adr/0033-pluggable-agent-runtime-acp.md) |

#### The fleet deck: `protoagent fleet` with no arguments

Bare `protoagent fleet` (or `protoagent top`) opens an interactive terminal over the
running hub — the **fleet deck**. The roster shows every member with the console's
presence words (host, online, remote, stopped, unreachable), version skew, spend over the
last 24 h, and the hub's runtime warnings as a banner. Keys: `enter` (or `c`) talk to the member, `i` member detail, `w` the work feed, `n` new
member, `R` rename, `d` delete, `a` add a remote, `e` edit a remote, `J`/`K` move a row (with
no filter active), `H` every hub on the box, `F5` refresh, `s`
start, `x` stop, `r` restart, `l` follow logs, `o` open the member in the browser console,
`/` filter, `?` help, `q` quit (members keep running). The footer lists only the keys that
apply to the selected row. Member detail shows runtime status (model, identity, warnings),
a following tail of the member's bounded, redacted log ring, the session inventory, and
the telemetry rollup — each pane degrades on its own if that read fails.

**Talking to a member.** `enter` (or `c`) on an online member opens a conversation.
The transcript streams your messages and the member's answers; the WORK pane lists every
tool call of the current turn as it happens — a subagent's own calls nested under its
`task` card — with args, result, and duration; `enter` on a card shows the full args and
result. Thinking folds behind a one-line count (`ctrl+z` unfolds). `esc` cancels a running
turn (then backs out); `ctrl+n` starts a new session; `ctrl+s` lists the member's console
sessions and replays one, tool cards included. Sessions use the console's own id shape,
so a conversation started here is waiting in the browser and vice versa. A stream that
goes silent for 45 s is checked against the member's durable task and finalized from it
only if the server already finished — never fabricated.

**Acting on a turn.** When the member parks on a question, a form, or an approval, the
status line says so; `enter` on the empty composer (or `ctrl+r`) opens it — a plain
question also takes whatever you type as the answer. Approvals are `a` / `d`; a form is a
stepped wizard (`ctrl+→` / `ctrl+←`, `ctrl+s` submits) with the console's own rules for
required fields, choices, and conditional fields; `ctrl+d` dismisses a request the way the
console does, so the turn never stays parked forever. Typing while the member is working
STEERS the running turn: the message queues and folds in at the member's next model call;
`up` on the empty composer pulls the newest queued message back to edit, and anything the
turn ended without reading is re-sent as a fresh turn. `ctrl+x` cancels the selected
running `task` card — that one delegation, not the turn. While a conversation is open the
session is *attended*: a scheduled or inbox turn in it parks on a question instead of
auto-answering, and the deck attaches to it as it runs (a turn already running when you
open a session is attached too); when the member says the turn is operator-controllable,
the composer interjects into it. `esc` on an attached turn detaches and backs out — it
never cancels somebody else's turn. (`ctrl+x` is the composer's *cut* while the composer
has focus; `tab` to the WORK pane first.)

**Managing members.** `n` creates a member: a name, an archetype from the hub's catalog
(the built-in Basic and every installed archetype, with what each installs and needs),
"inherit the hub's model connections and credentials" (on by default — the member boots
ready to chat) and "start after create". `R` renames the selected member's display name
only (letters, digits, `-` and `_`, like every member name) — its id, URL slug and data never
change, so open windows survive. `d` deletes it:
the member is stopped first, you type its name to confirm, and purging its workspace and
data is a separate checkbox — both irreversible, and the deck says so. If the hub reports
that the member stopped but its workspace survived (a 409), the deck says so and asks you
to repeat the delete; that is a partial result, not a failure. `a` registers a remote
protoAgent (name, URL, an optional bearer typed masked, sent once and never shown again);
`e` edits one in place (blank bearer keeps the stored one, "clear" forgets it — one or the
other, not both); `d` on a remote only unregisters it. An unreachable remote reads `unreachable`, never `stopped`.
`J`/`K` move the selected row and persist the order on the hub as a complete permutation
of member ids. The status line shows the hub's warm-agent cap (`fleet.warm.max`;
read-only here — change it in the hub's settings).

**Every hub on the box.** `H` (or `protoagent fleet --all`) lists the hubs this machine
runs — the desktop app's, `~/.protoagent`, each scoped instance under it — and peers found
on the LAN or the tailnet: one row per hub with its state, how it was launched (desktop
app, `protoagent up`, foreground), port, version and member counts, then its instance
root. Running hubs come from the `.instances/` heartbeats under every known box root;
stopped ones from every instance root that carries a `workspaces/fleet.json` (a member's
root is never a hub row). None of it depends on the shell's `PROTOAGENT_*` environment.
A running hub is probed with its own fleet token: one that answers but refuses every
credential reads `unauthorized` (pass `--token`), one that does not answer `unreachable`.
`enter` attaches the deck to that hub — the roster, feed and conversations then belong to
its fleet; `u` on a stopped hub runs `protoagent up` for that instance root and attaches
once its port answers. Stopping a hub is not a deck action (`protoagent down` in that
instance). Two hubs claiming one port both say so — ports are box-global.

**The work feed.** `w` lists what the fleet is doing — every member's server-fired turns,
tool calls, room replies, spend, and parked questions, folded from the members' event
buses into one time-ordered feed. `f` filters, `p` pauses, `enter` opens the member's
conversation at that row's session. The roster's TURN column follows the same events, and
the deck rings the bell when a member newly needs you.

The deck follows the same live/offline rule as the verbs below: with no hub answering it
shows this instance's `fleet.json` badged `offline`, and only start/stop are available.
Textual is imported only when the deck opens, so `--help` and the non-interactive verbs
stay fast; a build without it prints a one-line hint and exits 2.

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
protoagent fleet new scout --archetype pm       # from the hub's catalog (live); --bundle <git-url> works offline too
protoagent fleet new blank --no-start --no-inherit
protoagent fleet rename scout scout-prime        # display name only (letters, digits, - and _); the id and slug stay
protoagent fleet rm scout --purge               # asks you to type the name; --yes off a terminal
protoagent fleet remote add ava https://ava.tail:7870 --bearer-stdin < token.txt
protoagent fleet remote edit ava --url https://ava2.tail:7870 --clear-bearer
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
