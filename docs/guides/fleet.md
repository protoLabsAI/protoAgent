# Fleet — many agents on one host

Run several named agents on one machine, each fully **isolated**, each runnable in the
**background**, each built from a reusable **archetype** — and switchable in place from
**one console** (slug-routed, per-agent layout/theme). The fleet is a handful of composable
primitives:

| Primitive | What it is | ADR |
|---|---|---|
| **Workspace** | a named agent — its own config, secrets, plugins, scoped data, port | [0041](../adr/0041-workspaces-and-tiered-stores.md) |
| **Bundle** | a curated, pinned set of plugins installed as one | [0040](../adr/0040-plugin-bundles.md) |
| **Archetype** | a starter *agent type* in the new-agent picker — a persona plus an optional bundle; the shipped catalog plus any installed bundle that declares one. Ships in an **archetype repo** (`cowork-archetype`, `social-archetype`, …; the old "stack" term is retired) | [0100](../adr/0100-agent-archetypes.md) |
| **Tiered stores** | per-agent private data + an opt-in shared **commons** | [0041](../adr/0041-workspaces-and-tiered-stores.md) |
| **Supervisor** | run agents as persistent background processes (start/stop/status) | [0042](../adr/0042-fleet-supervisor-unified-console.md) |
| **Unified console** | one slug-routed console that hot-swaps between running agents (per-agent layout/theme) | [0042](../adr/0042-fleet-supervisor-unified-console.md) |
| **Fleet deck** | the same fleet in a terminal — roster, conversations, parked questions, management, every hub on the box ([guide](./fleet-deck.md)) | [0075](../adr/0075-external-interfaces-cli-mcp-api.md) |

## Quick start

```bash
# an agent from a bundle-backed archetype (project-manager-archetype: board-driven PM) —
# --input answers the bundle's Configure prompts, --soul seeds the persona the picker would
python -m server workspace new pm --bundle https://github.com/protoLabsAI/project-manager-archetype \
  --input project_board.repo=/abs/path/to/repo --input project_board.coder=claude-code \
  --soul config/soul-presets/project-manager.md

# a blank-slate agent (the built-in Basic archetype — core loop + tools, no plugins)
python -m server workspace new scratch

# run the whole fleet in the background, then look at it
python -m server fleet up
python -m server fleet ls
#   ● pm        :7871  pid 12345  [project-manager-archetype]
#   ● scratch   :7872  pid 12346

# …or open the fleet deck: the same fleet in a terminal (q quits; members keep running)
python -m server fleet
```

## Workspaces — a named, isolated agent

A **workspace** is a directory that *is* an agent. Its `langgraph-config.yaml`,
`secrets.yaml`, `plugins.lock`, and `config/plugins/` live there (so
`PROTOAGENT_CONFIG_DIR=<ws>` is its whole identity), and `instance.id = <name>` scopes its
**private data** (goals, chat history, memory, knowledge) to `~/.protoagent/<name>/*` — so
agents on one host never collide (the leak that motivated this; see
[multi-instance](./multi-instance.md)).

```bash
workspace new <name> [--from <cfg>] [--bundle <url>] [--input KEY=VALUE …] [--soul FILE] [--port auto] [--shared-skills]
workspace ls
workspace run <name>          # foreground: execs the normal server, env wired in
workspace rm <name> [--purge] # --purge also deletes its scoped data
```

`--from <dir>` clones an existing agent's config + secrets (re-stamping identity/instance);
`--bundle <url>` installs a bundle into it (next section); `--port auto` picks a port that is free
and that no member of **any** instance on this machine records — ports are machine-wide, and a
stopped member of another instance still owns its port (the pick and its record happen under one
machine-wide lock, so two hubs creating members at once cannot collide).
`--input KEY=VALUE` (repeatable, core ≥ 0.146, #2977) answers a bundle's `config_inputs`
prompts — the required ones must be answered or the create is refused, a `KEY=VALUE`
without `=` is a usage error, and a `type: delegate` answer is copied from the host config
(the CLI runs inside the host) **without** inheriting the host model. `--soul FILE` writes
the persona the picker would have seeded; without it the workspace has **no persona**, and
the CLI never records an archetype's capability contract. The picker and
`POST /api/fleet` do all of that in one step
([the body](./build-with-a-coding-agent#_1-stand-up-the-pm)).

Unlike the CLI path, the new-agent picker and `POST /api/fleet` default to
`inherit_config: true`: they carry the host's effective model and provider registry plus
nonblank secrets from the model/provider namespaces. protoAgent-owned OAuth is shared from
the box store; an older instance-local store is transferred there, never copied. Promotion
refuses a conflicting box login or a residual local override marked disconnected instead
of choosing a credential silently. Set `inherit_config: false` for no model/provider-secret
inheritance and no legacy OAuth transfer; an existing box OAuth store remains host-shared.

## Bundles & archetypes — start from a type

A **bundle** ([ADR 0040](../adr/0040-plugin-bundles.md)) is a repo whose
`protoagent.bundle.yaml` names a *pinned set of plugins* to install together, plus a
suggested enable list + config — the full lifecycle (manifest reference, updating,
uninstalling, publishing an archetype repo) is in the [Bundles guide](./bundles.md). Install
one into a workspace and you skip the plugin-by-plugin setup:

```bash
python -m server plugin install https://github.com/protoLabsAI/project-manager-archetype   # fans out + pins each member
```

A bundle that carries an **`archetype:`** block becomes a **starter agent type** the
new-agent picker offers — additive metadata, no change to the bundle shape:

```yaml
# protoagent.bundle.yaml
id: project-manager-archetype
plugins: [ … ]
enabled: [ … ]
archetype:
  label: Project Manager
  icon: ClipboardList
  blurb: Board-driven project manager — reads deeply, never holds the keyboard; ships through a verdict-gated board of coding agents.
  # optional persona: inline markdown, or a host preset stem under config/soul-presets/
  # (unknown preset → warns + the picker falls back to the base persona)
  soul_preset: project-manager
  # optional picker placement: "standard" (default, inline card) or "advanced"
  # (collapsed under the picker's "Advanced (N)" toggle)
  tier: advanced
  # optional: host capabilities the archetype needs to be USEFUL — the picker warns at
  # choose-time when one isn't provisioned (e.g. cowork's document skills route through
  # execute_code, which on the desktop app needs the managed Python runtime, ADR 0094)
  requires: [python_runtime]
  # optional capability contract (#2277): tool names the persona commits to using —
  # persisted to the created agent's workspace.yaml, checked against its bound
  # toolset at boot (advisory warning, never a gate)
  requires_tools: [github_create_issue]
```

Unknown keys in the block warn at install time; the full annotated field set lives in
`examples/bundles/template/protoagent.bundle.yaml`.

The picker draws from **two** sources:

- **The archetype catalog** — `config/archetype-catalog.json`, served by `GET /api/archetypes`.
  The shipped catalog carries the starter set (Basic, Cowork, Engineer, and — under the
  *Advanced* toggle — Design System Engineer and Project Manager, then Custom; the two
  code-free personas are Basic and Custom, the rest reference published archetype repos)
  and is **data-driven**: add or remove archetypes by editing the JSON, no code change. A
  row the operator has pulled from the picker without deleting it sits in the file's
  `held:` array (Social Marketing is parked there today) — held rows are never served. A fork or instance overrides it by
  dropping its own `archetype-catalog.json` in the live config dir (same rule as
  `plugin-catalog.json`); if the file is missing entirely, a hardcoded Basic + Custom
  fallback keeps the picker from rendering empty. Each entry names a `soul_preset` (a
  file under `config/soul-presets/`) or an inline `soul` for the base persona.
  `GET /api/archetypes/{id}/preview` peeks a bundle archetype's members/MCP
  servers/secrets before anything installs.
- **Installed bundles** — any bundle whose manifest carries an `archetype:` block
  **self-registers** on top of the catalog (deduped by id + bundle URL). Install the bundle
  and its starter type appears in the picker for free — no catalog edit needed.

Creating from an archetype is two steps, the same in Settings ▸ Fleet ▸ New agent and in the
first-run Setup Wizard: **pick** a card (label, blurb, *What's included*), then **set it up** —
the name (pre-filled from the archetype, e.g. `engineer`), the bundle's `config_inputs`
questions, and a collapsed **Advanced** section with the bundle's MCP inputs / secrets and the
persona. **Back** returns to the cards with every answer kept. The archetype seeds the new
agent's **persona** (its `SOUL.md`) and — if it carries a bundle — installs the bundle's
plugins into the new agent. See [Install & publish plugins](./plugin-registry.md).

### The Engineer archetype — a navigator, not a solver

**Engineer** ([engineer-archetype](https://github.com/protoLabsAI/engineer-archetype)) is a
hands-on pair-programming *navigator* for the operator's own machine: point it at a repo
(a git URL or a folder), and it clones or registers it, shows you its README and manifest,
draws a code-linked architecture overview (click a node to open its code), proves the
toolchain, writes a short repo card, and then works through a problem **one checkpoint per turn** — reproduce, narrow,
*your* hypothesis first, you type the fix, it reviews and runs the checks, you commit. It
never edits or commits unless your latest message says so. The design follows research that
passive, delegated AI help costs the operator comprehension; the bundle's README has the
references.

What it turns on: the in-tree `engineer` skill pack (`repo-onboard`, `debug-loop`; off for
every other agent), `craft`, `friction`, `delegates`, and the
[terminal plugin](https://github.com/protoLabsAI/terminal-plugin) — the operator's own shell
beside chat, not an agent tool — and the [github plugin](https://github.com/protoLabsAI/github-plugin)
**read-only** (issues, PRs, diffs, CI over `gh`; `github.write` stays off unless the operator
flips it, and even then the persona only posts when asked). Its recommended config enables the filesystem with
per-command approval (read-only `git` commands auto-approved via
`filesystem.run_auto_approve`), the code pane (`filesystem.code_pane`, so `show_code` can
point at exact lines), `open_in_editor` for Zed (`filesystem.editor_command: zed`; change it
to `code -g` / `cursor -g`), and project onboarding under `~/code` from any GitHub repo
(Settings ▸ Capabilities ▸ Project onboarding narrows or moves it). The Configure step asks
two optional things: a local checkout to start in (registered as a project and used as the
terminal's starting directory) and whether the GitHub write tools bind. The model is left to your host. Pair it with Zed's Agent
Panel through the `protoagent-acp` shim ([ADR 0111](../adr/0111-zed-operator-editor-acp-shim.md)).

## Tiered stores — private by default, share what should be shared

Each agent's stores are **scoped** (private) by default. **Skills** can be tiered so a fleet
shares a growing skill library while keeping the rest private —
[ADR 0041](../adr/0041-workspaces-and-tiered-stores.md):

```yaml
skills:
  scope: scoped | shared | layered   # default: scoped
commons:
  path: ""    # shared-tier base dir; blank → ~/.protoagent/commons
```

- **scoped** — a private skills DB per agent.
- **shared** — one commons DB the whole fleet reads *and* writes.
- **layered** — *shared brain, private hands*: read the commons ∪ your private library, but
  **writes go to private**, so half-baked learned skills never pollute the fleet. Lift a
  proven one up explicitly:

```bash
python -m server skills ls               # private + commons, tagged by tier
python -m server skills promote <name>   # a private skill → the shared commons
```

## The supervisor — agents in the background

Run the fleet as persistent background processes — [ADR 0042](../adr/0042-fleet-supervisor-unified-console.md):

```bash
python -m server fleet up [names…]    # start agents — all workspaces, or named
python -m server fleet ls             # ● running / ○ stopped + port + pid
python -m server fleet down [names…]  # stop agents
```

Each agent is an ordinary headless server (`--ui none`) on its workspace's port, tracked in
a `fleet.json` registry. Because each agent's chat history is scoped to its own
checkpoints, a **stopped agent's session resumes** when you restart it — and a **running**
one keeps its background work (schedules, an in-flight loop) going while you're elsewhere.

## Deploying a team — config-as-code (`fleet.autostart`) {#deploying-a-team}

The commands above are **imperative** — you create members and start them by hand. That's
fine at the console, but a team you *deploy* (a lead plus the specialist members it delegates
to) should be a **config-as-code artifact**: baked into an image, versioned, and stood up with
one `docker compose up`. Two pieces make that real.

**1. Declare the crew — `fleet.autostart`.** A container recreate (an image roll) or a host
restart kills the members' detached processes; `fleet.json` survives in the volume with
now-dead pids, but nothing restarts them — so a hand-assembled crew silently stays down until
you re-activate each one. List the members the hub should keep up and it **(re)starts them on
boot**:

```yaml
fleet:
  autostart: [cindi, matt]   # member ids or display names
```

(or `PROTOAGENT_FLEET_AUTOSTART=cindi,matt` in the environment). It runs right after the boot
version-reconcile: **idempotent** (an already-running member is skipped), **best-effort** (a
missing workspace or a failed spawn is logged and skipped — never blocks boot), and **hub-only**
(a member's own scoped config carries no roster, so it no-ops inside a member).

> Autostart restarts the member **process**; it does not by itself resume the **work** that
> process was doing. A member whose job is a long-running background loop (a trading engine, a
> poller) needs its *plugin* to resume that loop when it boots — see
> [surfaces that resume across reloads](./plugins.md#surface-resume). The two compose: autostart
> brings the agent back, the surface pattern brings its work back, and a host restart recovers
> the whole crew with no manual steps.

**2. Bake the lead + personas as seeds.** The lead's own config and persona seed from
`PROTOAGENT_SEED_CONFIG` and `PROTOAGENT_SEED_SOUL` on first boot (seed-not-force — operator
edits persist), so the whole team ships in one image with no wizard. See
[Deploy with Docker](./deploy-docker.md) for the seed pattern and the volume-shadow traps it
avoids — in particular, add `PROTOAGENT_SEED_MERGE=1` if you expect to keep *changing* the
baked config: without it, seeding is first-boot-only, so a member with a persisted config
volume will go on serving the identity and card it was originally seeded with no matter what
later images bake.

Put together, a deployed team is **lead config seed + persona seed + `fleet.autostart` roster +
[`delegate_to`](./delegates.md) wiring** — `docker compose up` brings up the lead, restarts the
crew, and hands real work to them (delegation is reliable past 60s as of
[#1788](https://github.com/protoLabsAI/protoAgent/pull/1788); a member's turn is bounded by its
delegate `poll_timeout_s` of *no observable progress*, not by total length — a turn that keeps
streaming work is waited out).

> **Reproducible-from-zero is the next step, not this one.** `fleet.autostart` reconciles members
> that already **exist** (created via the console, `POST /api/fleet`, or `--from`/`--bundle`) and
> references them by id/name — so a full volume wipe that must recreate them from scratch is out of
> scope. Creating members from archetypes, auto-deriving the lead's delegates, and a shared commons
> — a whole team from a single baked manifest — is [ADR 0072](../adr/0072-fleet-seed-team-via-config.md)'s
> `PROTOAGENT_SEED_FLEET`; `fleet.autostart` is its first shipped slice.

## The unified console — every agent in one UI

*(Shipped — ADR 0042 slices 2–5.)* The **hub** (any running agent) serves one console and
reverse-proxies each agent window's chat / A2A / SSE / WebSockets to that agent's backend,
keyed by the **URL slug** (`/app/agent/<id>/`) — so every window targets its own agent:
switch in place from the topbar, or open two agents in two windows at once. Per-agent chat,
theme and layout follow the slug; a stopped agent **resumes from its checkpoint** when you
navigate to it; "+ New agent" runs the archetype picker. A plugin view served by a member
that opens a **WebSocket** (e.g. `agent_browser`'s live viewport) works through the hub too:
the slug proxy forwards WS upgrades, not just HTTP/SSE ([#883](https://github.com/protoLabsAI/protoAgent/issues/883), shipped v0.35.0). Settings → Agents is the fleet manager
(create / start / stop / rename / remove), and **Discover** finds other protoAgents on the
box, the LAN (mDNS) and your **tailnet** (via the Tailscale CLI). **mDNS is off by default**
([#1802](https://github.com/protoLabsAI/protoAgent/issues/1802)) — an agent stays quiet on the
network and won't announce itself over LAN Bonjour unless you enable `fleet.discovery.mdns`
(Settings → Host → Discovery), a privacy/security-first default. Local-box and tailnet discovery
and manual register are unaffected, and the fleet console still lists your own members (it reads
them from disk, not mDNS). To flip a local member
on or off without opening Settings, press **⌘⇧K → Fleet Room** and use the start/stop
control on that member's roster row (only local members get one — never the host, a remote
member, or the agent serving the window you're in) — see
[command palette](./command-palette.md).

**Fleet settings are hub-only.** The topbar dropdown's **Fleet settings** item is enabled
on the host window (and on a standalone instance — that's where you create your first
member); in a *member* window it renders **disabled** with a tooltip pointing you at the
host instance. That covers both a member's slug window and a spawned workspace member
opened directly on its own port — the member self-reports `member: true` on its own
`GET /api/fleet` host entry (its instance root carries the `workspace.yaml` spawn marker).
A **remote** member opened at its own URL stays enabled on purpose: it's an independent
instance that may run its own fleet, and registration is one-sided on the hub.

## Remote fleet members — the agent there, the UI here

*(ADR 0042 §I.)* A fleet member doesn't have to be local: register any reachable protoAgent
by URL and it becomes a **switchable member** — a slug window like any peer, with the hub
reverse-proxying its console + A2A. The remote runs fully headless; this console is its UI.
The end-to-end walkthrough (making the remote reachable, pairing it by code, revoking,
the `auth` badges and delegating through the hub) is [Pair devices and agents](./pairing.md).

On the other machine:

```bash
A2A_AUTH_TOKEN=<secret> python -m server --port 7871 --host 0.0.0.0 --ui none
```

On this one — Settings → Agents → **Discover** → **➕ Add to this fleet**, or register
manually (the stored token is attached by the proxy; the browser never sees it):

```bash
curl -X POST http://127.0.0.1:7871/api/fleet/remotes \
  -H 'content-type: application/json' \
  -d '{"name": "ava", "url": "http://100.101.189.45:7871", "token": "<secret>"}'
```

**Pairing instead of pasting a token** *(ADR 0113)*. Rather than handing the hub the
remote's shared bearer, have the remote's operator generate a pairing code on it
(Settings ▸ Devices) and claim it from the hub:

```bash
protoagent fleet pair http://100.101.189.45:7871 <code> [--name ava]
# or: curl -X POST http://127.0.0.1:7871/api/fleet/remotes/pair \
#       -H 'content-type: application/json' -d '{"url": "http://100.101.189.45:7871", "code": "<code>"}'
```

The hub redeems the code against the remote's `POST /api/pairing/claim` and stores the
per-device token it gets back. The remote lists the hub as `<hub name> (fleet hub)` in its
Devices and can revoke it on its own. What happens next depends on the URL:

- **The URL is already a member.** Its token is **replaced** in place: this is the re-pair
  after a revoke. If the old token still works, the device it was issued for is revoked on
  the remote, so re-pairing doesn't pile up live operator devices there.
- **The URL is new.** The member is added, named after the remote's agent card (with `-2`,
  `-3`… added if that name is taken). An explicit `--name` that is already taken is refused
  *before* the single-use code is spent.

A wrong or expired code is a 400. A remote that is unreachable, or doesn't reply the way a
protoAgent that supports pairing would, is a 502.

**Cleartext needs an explicit yes** *(ADR 0113 D10)*. A pairing code, and the token it
turns into, cross plain `http://` without asking only when the address is safe:

- loopback;
- a tailnet: `100.64.0.0/10`, Tailscale's IPv6 range, or a `*.ts.net` MagicDNS name. A
  tailnet is WireGuard-encrypted underneath.

Any other name is judged by the addresses it resolves to at pairing time; every one of them
must be safe. Plain `http://` to anything else is refused with a 400 that names the fix:
use the remote's tailnet address or `https://`. To proceed anyway on a network you trust,
opt in with `allow_insecure: true` on the API, or `--insecure-http` on the CLI (the same
flag that allows a plain-http `--hub`).

The same rule applies when a token is being stored by "Add a remote by URL" or by an edit.
Registering an address with no token sends nothing, so it is never gated. This doesn't make
LAN pairing safe. It makes it a decision the operator takes knowingly. TLS on the remote is
the real fix.

The URL is the agent's **base** URL: `scheme://host[:port]`. Scheme and host are
lowercased and a default port (`:80` / `:443`) is dropped, so two spellings of one remote
are one member. A path, query, fragment or `user:pass@` is refused. That includes a phone
pairing link (`…/app/#pair=…`): use the base URL with an agent code instead.

**A token never follows a moved URL.** If a remote's URL is edited to a different
scheme, host or port without a new token in the same edit, the stored token is **cleared**,
and the answer says `token_cleared: true`. The token was issued by the old host, and the
hub won't present it to a different one. Pair again, or pass a token with the edit.

**Token health.** When a remote has a stored token, the hub checks it with `GET
/api/devices` on the remote, every 30s and at once after an add, edit or pair. It first
asks without the token, and reports `auth` on the member:

| `auth` | Meaning |
|---|---|
| `ok` | The remote requires auth, and the token passes. |
| `rejected` | 401/403 with the token: it was revoked or is wrong. Re-pair. |
| `open` | The remote answered *without* any token. Its auth is off, so the token can't be verified, and anyone who can reach it can drive it. |
| `unknown` | No verdict yet, or an older remote without that route. |
| `none` | No token is stored. |

Remote members show a `remote` tag + their URL in the fleet manager; `running` is a cached
reachability probe. You can't start/stop/rename them from here — their deployment owns
their lifecycle; **Remove** only unregisters (the remote agent is untouched). Registering
as a member and adding as a [`delegate_to` target](delegates.md) compose: the same agent
can be both a window you operate and a delegate your agents call.

**"Add as delegate" on a remote routes through the hub** *(ADR 0113 D4)*. A remote's
advertised A2A endpoint (the roster's `a2a` field, which "Add as delegate" wires) is the
hub's own loopback proxy, `http://127.0.0.1:<hub-port>/agents/<remote-id>/a2a`, not the
remote's URL (that stays in `url`). The delegate carries no token: on loopback it presents
the fleet service token, the hub accepts it, and the proxy swaps in the remote's **stored**
token. So the remote's credential lives in exactly one place — this fleet row — and
re-pairing or editing its token fixes the window and every delegate at once; it never leaves
the hub's machine. A remote with **no** stored token is proxied with no credential at all (the
hub never forwards its fleet token off the box), so a secured one answers `401` and the
delegate's error tells you to pair it. Local members can use the same URL (they hold the fleet
token too), so it also works as a [fleet-shared delegate](delegates.md#share-a-delegate-with-the-whole-fleet-adr-0105).

**Live views (WebSockets) work on a remote too** — the terminal, agent_browser's viewport —
as long as the remote was registered **with a token** (ADR 0113 D6,
[#3648](https://github.com/protoLabsAI/protoAgent/issues/3648)). The hub still never lends
that token on its own. A socket that presents your operator bearer (`?token=`) has it checked
at the hub and swapped for the stored one. An **open** hub (no token of its own) has nothing
to check it against, so it never swaps. A ticket-based socket (the ticket is minted over the
authenticated HTTP proxy) reaches the remote with no credential attached, and the remote
checks the ticket. Any other presented credential is closed with `1008`, and so is a browser
page on a foreign origin: the console's own origin, the desktop app and `A2A_ALLOWED_ORIGINS`
are let through. A remote registered without a token gets no WebSocket proxying at all,
because its sockets would be a blind pipe into an open instance. The hub doesn't read what a
plugin sends *inside* the socket, so remote terminal views need **terminal-plugin ≥0.9.2**.
Older versions sent the console's bearer in-band when minting a ticket failed.

**An open hub only proxies a remote for its own console** *(#3662)*. On a hub with no token
of its own (the desktop default on loopback) every caller is operator, so the hub lends a
remote's stored token to whatever reaches `/agents/<remote>/…`. CORS stops a web page in your
browser from *reading* the answer, not from *sending* a form POST there, and a DNS-rebinding
page (an attacker's name that resolves to `127.0.0.1`) is even same-origin with the hub. So for
a **remote** member on an **open** hub, the proxy refuses with `403` before contacting the remote:

- a request with `Sec-Fetch-Site: cross-site`, unless it carries a trusted `Origin`, is a
  GET navigation (a link, the desktop app's plugin-view iframe), is a GET media load (an image,
  audio, video, subtitle track or font, like the desktop chat's pictures from a remote), or
  has a trusted `Referer`;
- a request whose `Origin` is not the hub's own origin, the desktop app
  (`tauri://localhost`, `http://tauri.localhost`) or in `A2A_ALLOWED_ORIGINS`;
- a request (or WebSocket) whose `Host` isn't a name this hub is served under: IP
  literals, `localhost`, `*.ts.net`, this machine's `<name>.local` mDNS name, a named bind,
  the hosts of `A2A_ALLOWED_ORIGINS`, or `PROTOAGENT_TRUSTED_HOSTS`. If you front an open hub
  with a reverse proxy that forwards its own public name, add that name there.

curl, scripts and the `delegate_to` path send neither header and pass. Local members and the
host skip the `Origin` and Fetch Metadata checks, and a token-gated hub skips all of them. Its
credential is a header, never a cookie, so a foreign or rebound page has nothing to attach.
The `Host` check isn't specific to this proxy: an open instance applies it to every path
([Security & trust](/explanation/security-and-trust#an-instance-with-no-token)).

**Version skew is flagged.** The hub console drives a remote's full `/api/*` by proxy, so a
remote on a *different protoAgent release* is a real compat surface. The reachability probe
also reads the remote's app version off its A2A agent card; when it differs from the hub's,
the fleet manager shows a warning badge on that member ("remote runs vX.Y.Z, the hub
vA.B.C — features may misbehave"). Upgrade the lagging side to clear it.

## Fleet diagnostics — let an agent read a member without touching it

*(ADR 0071 · #3170.)* A managing agent can inspect a fleet member's health without any
runtime control: the **`fleet_diagnostics`** tool reads a member's **recent logs** and **one
exact task by id**, and nothing else. It is **off by default** — turn it on with a single
config knob:

```yaml
tools:
  fleet_diagnostics:
    enabled: true   # default: false — binds the read-only fleet-diagnostics tool
```

(Settings ▸ Tools ▸ *Expose fleet diagnostics to the model*.) What the tool can and cannot do,
by design:

- **Read-only, two reads.** `read="logs"` returns a bounded tail of a member's recent log
  lines; `read="task"` returns one exact A2A task by id. There is **no** path to start/stop a
  member, resume or answer a task, mutate a checkpoint, prompt a human (HITL), or change any
  configuration — a mutation request comes back as a structured refusal, never an action.
- **Roster-only addressing.** The target is resolved **exclusively** through the configured
  fleet roster (the same host + local-peer + remote members every console surface reads). The
  agent names a member by display name or id; there is no parameter — and no code path — that
  accepts a host, port, or URL, so the model can never point it at anything the hub does not
  already own. An unknown member is a refusal.
- **Bounded and secret-redacted, on success and error.** Every read is line/field-capped and
  secret-redacted at the tool's own boundary (defense in depth over the member's own bounding),
  and a stopped / unreachable / slow member or a missing task id returns a compact structured
  `{"ok": false, "error": …}` rather than a stack trace or an unbounded dump.

**Exposing it to a foreign operator-MCP client.** The operator-MCP `read-only`
[profile](./mcp.md) lists `fleet_diagnostics` as a **candidate** so a foreign
client (Claude Desktop, Cursor, an ACP sidecar) can receive this read-only capability. But
**profile membership never enables the tool by itself** — the `tools.fleet_diagnostics.enabled`
gate above remains the sole authority on whether the tool is bound at all. With the gate off,
the tool is absent from every surface — the local model's toolset, the resolver, the
`/api/mcp/exposed` discovery route, and the ACP sidecar — regardless of the selected profile.

## See also

- ADRs: [0040 bundles](../adr/0040-plugin-bundles.md) ·
  [0041 workspaces & tiered stores](../adr/0041-workspaces-and-tiered-stores.md) ·
  [0042 fleet supervisor & unified console](../adr/0042-fleet-supervisor-unified-console.md) ·
  [0072 fleet seed / team-via-config](../adr/0072-fleet-seed-team-via-config.md)
- Guides: [deploy with Docker](./deploy-docker.md) · [delegates](./delegates.md) ·
  [multi-instance scoping](./multi-instance.md) · [plugins](./plugins.md) ·
  [install & publish plugins](./plugin-registry.md) · [skills](./skills.md) ·
  [operating a fleet (health, rollout, triage, recovery)](./operating-a-fleet.md) ·
  [the fleet deck (a terminal for the fleet)](./fleet-deck.md)

## What the box shares with every member

Every agent on the machine reads the box's `host-config.yaml` (the Host layer of
the settings cascade, ADR 0047 — gateway/model defaults) and, since ADR 0105, its
`delegates:` list: a delegate the hub saved with **Share with fleet** is on every
member's bench without a copy, its secrets in the owner-only `host-secrets.yaml`
beside it. Members read both files and never write them; the hub's Settings ▸
Delegates is the editor. The `dev` sandbox shares the same box.
