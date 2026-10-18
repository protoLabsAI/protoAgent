# Build plugins

Build a plugin to add tools, skills, routes, background work, or console views
without changing core. Plugins run in the server process with its privileges.
Enable only code you trust. Some bundled plugins are enabled by default; the
`delegates` registry is built in and needs no enable step.

To use an existing plugin, follow [Install and manage plugins](/guides/plugin-registry).
Telegram is bundled; Discord, GitHub, Google, and Slack install from external repos.

## Start here

For a first working example, use [Build your first plugin](/tutorials/first-plugin).
This guide covers the contribution contracts. The references below are generated
from source signatures and docstrings:

| Reference | Contents |
| --- | --- |
| [Plugin manifest](/reference/plugin-manifest) | Every `protoagent.plugin.yaml` field — type, default, meaning |
| [Registry API](/reference/plugin-registry-api) | Every `register_*` seam with its exact signature, plus `registry.host` |
| [Plugin SDK](/reference/plugin-sdk-api) | `graph.sdk` — subagents, knowledge, watches, background jobs, metrics |
| [Testkit](/reference/plugin-testkit) | `load_plugin`, `FakeRegistry`, host stubs |
| [Plugin CLI](/reference/plugin-cli) | `python -m server plugin …` |
| [View bridge](/reference/plugin-view-bridge) | The sandboxed-iframe `postMessage` protocol |
| [Event bus topics](/reference/plugin-events) | Every topic core publishes, with payload keys |

For console UI, see [Build a plugin view](/guides/build-a-plugin-view). For messaging
integrations, see [Build a communication plugin](/guides/communication-plugins).
[Plugin architecture](/explanation/plugin-architecture) explains loading and trust.

## Anatomy

A plugin is a directory with a manifest and a module exposing `register(registry)`:

```
plugins/hello/
├── protoagent.plugin.yaml   # manifest
├── __init__.py              # def register(registry): ...
└── skills/                  # optional bundled SKILL.md skills
    └── greeting/SKILL.md
```

### Manifest — `protoagent.plugin.yaml`

```yaml
id: hello
name: Hello Plugin
version: 0.1.0
description: A small greeting tool.
enabled: false
```

**Every field, with its type and default: [Plugin manifest reference](/reference/plugin-manifest).** Generated from the manifest dataclass. The fields worth explaining rather than listing have their own sections below.

### Entry — `register(registry)`

```python
from langchain_core.tools import tool

@tool
async def hello(name: str = "world") -> str:
    """Return a friendly greeting."""
    return f"Hello, {name}!"

def register(registry):
    registry.register_tool(hello)        # expose a LangChain tool
    registry.register_skill_dir("skills")  # bundle SKILL.md skills (relative to the plugin)
```

`register` runs when the plugin loads. Choose the contribution needed for your task:

| Task | API or guide |
| --- | --- |
| Give the model a tool | `register_tool` / `register_tools` |
| Teach a procedure or advertise a capability | `register_skill_dir` / `register_a2a_skill` |
| Serve an HTTP endpoint or run background work | `register_router` / `register_surface`; see [reload behavior](#surface-resume) |
| Add model-call hooks or a completion check | [Middleware](#middleware-register-middleware-adr-0032) / [goal and watch verifiers](#goal-and-watch-verifiers) |
| Connect an MCP server | [Managed MCP servers](#managed-mcp-servers-—-register-mcp-server) |
| React to another component | [Event bus](#event-bus) / [lifecycle events](/guides/lifecycle-events) |
| Call the agent or persist plugin data | [Host services](#host-services-—-registry-host) / [SDK](#consumption-sdk) |
| Add a console view | [Plugin-view quickstart](/guides/build-a-plugin-view) |

The [registry reference](/reference/plugin-registry-api) owns the full method list,
signatures, and lifecycle contracts, including chat commands, knowledge backends,
embedders, and media storage. Use `live_config()` in long-lived handlers;
`registry.config` is the snapshot from registration.

#### Surfaces that resume their work across reloads {#surface-resume}

A surface's `start` runs on every server boot **and** after a plugin reload; `stop` runs on
shutdown/reload; `reload(cfg)` runs on a config save. So a surface that manages ongoing
background work — a trading loop, a poller, a long-running job — has a subtle obligation: a
reload/restart is **not** "the operator turned it off," so the surface must *resume* that work
rather than leave it stopped. Restarting the process alone doesn't bring the *work* back;
[`fleet.autostart`](/guides/fleet#deploying-a-team) restarts the **agent**, this pattern
restarts what it was **doing**. The recipe:

1. **Persist the operator's INTENT, not the run state.** When the operator starts the work,
   write a durable flag (`wanted: true`); when they *deliberately* stop it, clear the flag.
   Module-level state resets on reload, so the flag lives on disk — a small JSON in the
   instance store returned by `sdk.plugin_store(plugin_id=registry.plugin_id)`.
2. **Distinguish a lifecycle stop from an operator stop.** The surface `stop` hook
   (shutdown/reload) must halt the task **without** clearing the intent — a reload isn't a
   decision. Only the operator's own stop clears it. (A single `stop_ops()` that both halts
   *and* clears would make every reload read as "turned off," and it would never resume.)
3. **Resume in `start` when the intent stands.** The `start` hook reads the flag and
   re-launches the work if it was wanted; otherwise it stays idle.

```python
async def _start():                       # runs on boot AND after a reload
    if _intended():                       # persisted flag — survived the module reset
        _resume_work()                    # re-launch what the operator had running

async def _stop():                        # runs on shutdown/reload
    _lifecycle_halt()                     # stop the task, KEEP the intent (not an operator stop)
```

**Safety rule — the clear direction must fail *safe*.** The failure modes are asymmetric: a
lost *start*-write leaves the flag absent → no resume → safe; a lost *stop*-write leaves it
`true` → a reload would **resurrect deliberately-stopped work** → not safe. So verify the clear
landed (read it back), and if it genuinely can't persist, make that **loud** rather than
silent — never let a write failure resume work the operator turned off. protoTrader's
SpaceTraders plugin is the worked example: `st_autopilot_start` persists `autopilot_wanted`, a
`lifecycle_stop()` halts the trading engine on reload while keeping the flag, and the surface
`start` resumes it — so a host restart brings the *trading* back, not just the process.

### Managed MCP servers — `register_mcp_server`

A plugin can ship a **managed MCP server** the agent connects to, instead of
making the operator hand-edit `mcp.servers`. The factory is called at every graph
build with the live `LangGraphConfig`; return a `mcp.servers[]` entry (`{name,
transport, command, args, env, ...}`) when the server should run, or `None` when
it shouldn't (off / not yet connected) — so the server comes and goes with config.
A returned entry whose `name` matches a configured server replaces it, and a
factory that returns an entry activates MCP even when `mcp.enabled` is off. This
is how an integration plugin can ship an OAuth-gated MCP surface (e.g. a Google
Gmail/Calendar external plugin) without a core edit. For a frozen desktop build (no `python` on PATH),
launch via `args: ["--mcp-plugin", "<id>"]` and expose a `mcp_main()` in your
plugin module — the binary re-invokes itself and the shim runs it.

### Plugin-managed MCP example

#### Worked example — wrap an external server, gated on config

In the plugin's `__init__.py` (`register(registry)` is the contribution hook — see
[Plugins](./plugins.md)):

```python
def register(registry):
    def mcp_factory(config):
        # registry.config is this plugin's resolved config section (ADR 0019).
        token = registry.config.get("api_token")
        if not token:
            return None                       # not configured → server doesn't start
        return {
            "name": "acme",                   # same name as a user mcp.servers entry → replaces it
            "transport": "stdio",             # or "streamable_http" / "sse"
            "command": "npx",
            "args": ["-y", "@acme/mcp-server"],
            "env": {"ACME_TOKEN": token},
        }

    registry.register_mcp_server(mcp_factory)
```

Enable the plugin and set its `api_token` (Settings ▸ Plugins, or its config section +
`secrets.yaml`); the `acme` tools appear (namespaced, allowlist-gated like any MCP server)
on the next graph build and vanish when the token is cleared. The returned dict is the same
`mcp.servers[]` entry shape as a hand-configured server (`command`/`args` + `env` for
stdio; `url`/`headers` for http/sse).

#### A Python server inside the plugin (frozen apps)

If the plugin *implements* the server itself in Python (rather than wrapping an external
binary), it exposes an `mcp_main()` that runs the server, and the factory launches it as a
subprocess. In a **frozen desktop build** there's no `python` on PATH, so the agent
re-invokes its own binary with the generic **`--mcp-plugin <id>`** shim, which imports the
plugin and calls its `mcp_main()`. The first-party OAuth-gated Google (Gmail/Calendar)
plugin is the canonical example.


### Public paths — inbound webhooks & public assets {#public-paths}

Under a token-gated deployment the auth middleware is **default-deny**: every path needs the
operator bearer. That breaks two legitimate cases — an **inbound webhook** (a third party POSTs
you and can't send your bearer; you verify its own HMAC/signature instead) and a **public
asset/page** a browser loads with a plain navigation. Declare those paths in the manifest's
`public_paths` to exempt them from the gate:

```yaml
# protoagent.plugin.yaml
public_paths:
  - /api/plugins/stripe/webhook      # inbound POST — verify the Stripe signature yourself
```

Each entry **must** live under this plugin's own namespace — `/plugins/<id>/…` or
`/api/plugins/<id>/…` — with the trailing slash after the id segment. That scoping is the
security boundary: a plugin can exempt only its own routes, never a core path like `/api/config`
(the manifest parser drops anything else with a warning, and the auth layer re-checks it as
defence-in-depth). **You still own the auth** on an exempt route — validate the caller's
signature in the handler; the exemption only removes the bearer requirement. Console **view**
pages are auto-exempted (a view page is public chrome), so you don't list those here — only
webhooks and any non-view asset the browser must fetch anonymously. Its DATA stays gated under
`/api/plugins/<id>/*`.

### Federation paths — a peer-reachable plugin RPC {#federation-paths}

[ADR 0066](/adr/0066-goal-trust-operator-channel) splits credentials into two tiers: the
**operator** bearer (everything) and an optional **federation** token (`auth.federation_token`)
that is confined to `/a2a` + `/v1` — every `/api/…` path, plugin routes included, answers it
with 403. That ceiling is what stops a peer credential from being host code-exec via
`/api/plugins/install`. It also means a *second instance* that holds only the federation token
can't call a plugin's own route — so a plugin that wants a peer to sync a plugin-owned store
deterministically (a second device, a fleet PM reading a board) has historically had only two
bad options: hand the peer the operator bearer, or tunnel RPC through the A2A task envelope.

`federation_paths` is the third option (#2747). Declare the prefixes — under this plugin's own
namespace, same boundary as `public_paths` — on which the federation credential is accepted:

```yaml
# protoagent.plugin.yaml
federation_paths:
  - /api/plugins/room/v1/        # a versioned, plugin-owned RPC prefix
```

What changes, precisely: on a matching path the middleware lowers the tier **ceiling** from
operator to federation. Nothing else. The route is **not** public — a request with no or a
wrong credential is still 401 — and a path a plugin didn't declare (including its own other
routes, another plugin's identical shape, and the fleet-proxied `/active/<slug>/api/…` variant)
keeps the full ceiling. View pages are *not* auto-added here (they're chrome, not RPC).

Inside the handler, read the verified tier and bind identity **by tier**, never from the
payload:

```python
@router.post("/v1/sync")
async def sync(request: Request, body: SyncBody):
    tier = request.state.trust_tier          # "operator" | "federation"
    principal = LOCAL_PRINCIPAL if tier == "operator" else PEER_PRINCIPAL
    ...
```

Two consequences worth designing for. **Open mode has no federation tier** — with no
`auth.token` configured every caller is the operator, so a plugin that distinguishes a peer
should refuse to enable its peer identity until a bearer is set (`a2a_impl.auth.bearer_configured()`).
And there is **one federation token**, so the tier identifies *a* peer, not *which* peer;
per-token peer identity is the #1504 follow-up and should not be reinvented inside a plugin.

The set is replaced wholesale on every plugin reload (the same #1890 rule as `public_paths`):
disable or uninstall the plugin and its lowered prefixes vanish immediately — the router may
stay mounted until restart, but the path is operator-only again, so a peer gets 403 rather
than a stale door. The live set is visible alongside the public prefixes on the member
well-known path.

### Middleware — `register_middleware` (ADR 0032) {#middleware-register-middleware-adr-0032}

A plugin can contribute a LangGraph **`AgentMiddleware`** — the per-turn hook layer
(`before_model` / `after_model` / `wrap_tool_call` / …) the core uses for knowledge
injection, enforcement, compaction, and audit. The factory gets the live config and
returns a middleware instance (or `None` to opt out); it's appended to the chain just
before the internal message-capture middleware, so its hooks run and the turn is still
captured. The full hook inventory, chain order, a worked summarize-and-ship example,
and the design rules live in the [Middleware guide](/guides/middleware).

For **per-request** data (the A2A request's merged metadata — project scope, origin,
caller keys), read `current_request_metadata()` — a contextvar bound for the duration
of each turn. This is how a fork injects a per-turn directive without editing the core
executor:

```python
from langchain.agents.middleware import AgentMiddleware
from graph.middleware.request_context import current_request_metadata

class ScopeBannerMiddleware(AgentMiddleware):
    def before_model(self, state, runtime):
        project = current_request_metadata().get("project")
        if not project:
            return None
        banner = SystemMessage(content=f"Active project scope: {project}. Stay within it.")
        return {"messages": [banner, *state["messages"]]}

def register(registry):
    registry.register_middleware(lambda config: ScopeBannerMiddleware())
```

### Goal & watch verifiers — `register_goal_verifier` (ADR 0028) {#goal-and-watch-verifiers}

A plugin can **ground-truth its own domain state** as a verifier — an async
`(spec, ctx) -> VerifyResult` that a `{"type": "plugin", "check": "<plugin-id>:<name>"}`
[goal](/guides/goal-mode) or [watch](/guides/watches) dispatches to. `args` in the
spec are declarative data your verifier validates (no shell, no eval — which is why
`plugin` is the only verifier type an agent/plugin may set programmatically):

```python
from graph.goals import VerifyContext, VerifyResult

async def verify_credits(spec: dict, ctx: VerifyContext) -> VerifyResult:
    want = int(spec.get("args", {}).get("min", 0))
    have = await current_credits()             # in-process; state the plugin owns
    return VerifyResult(have >= want, f"credits {have:,}/{want:,}", evidence=str(have))

def register(registry):
    registry.register_goal_verifier("credits", verify_credits)   # → <plugin-id>:credits
```

**The `ctx` contract** (`graph.goals.VerifyContext`) is stable and grows only
additively — a verifier that ignores it keeps working:

| Field | Meaning |
|---|---|
| `config` | the live `LangGraphConfig` |
| `condition` | the goal/watch condition text |
| `last_text` | last assistant message of the turn (goals; `""` for a watch tick) |
| `tool_summary` | short summary of the turn's tool calls (goals; `""` for a watch tick) |
| `cwd` | working directory (used by the command/test verifiers) |
| `invoker` | **who is polling** — a `VerifierInvoker`, or `None` outside the goal/watch loops |

`ctx.invoker` (#1641) identifies the invoking controller, so one verifier can serve
many goals/watches without resorting to global state:

- `kind` — `"goal"` or `"watch"`.
- `id` — the invoker's id: a **goal** is keyed by its session (so `id == session_id`);
  a **watch** by its own watch id.
- `session_id` — the owning session: the goal's session, or the watch's
  `run_session` (`""` when the watch targets no session).
- `interval_s` — the watch's effective polling cadence (its `interval_s` override,
  else the config `watch_interval`); `None` for goals (they evaluate post-turn).

`VerifierInvoker` is a **frozen, hashable** dataclass — key per-invoker state by it.
E.g. a drawdown verifier keeping one high-water mark *per watch* instead of one
global mark:

```python
from graph.goals import VerifierInvoker, VerifyContext, VerifyResult

_marks: dict[VerifierInvoker | None, float] = {}

async def verify_drawdown(spec: dict, ctx: VerifyContext) -> VerifyResult:
    equity = await current_equity()
    mark = _marks[ctx.invoker] = max(_marks.get(ctx.invoker, equity), equity)
    frac = float(spec.get("args", {}).get("frac", 0.1))
    tripped = equity <= mark * (1 - frac)
    return VerifyResult(tripped, f"equity {equity:,.0f} vs mark {mark:,.0f}", evidence=str(equity))
```

To *react* when a goal/watch finishes, pair with `register_goal_hook` /
`register_watch_hook` — see [Goal mode ▸ Reacting to a goal](/guides/goal-mode#reacting-to-a-goal)
and [Watches](/guides/watches).

## Host services — `registry.host`

A surface or route often needs to **call the agent** or the **event bus** — host services it
can't build itself. `registry.host` exposes them:

```python
def register(registry):
    host = registry.host
    async def _on_message(text, sid):
        return await host.invoke(text, sid)        # call the agent
    registry.register_surface(lambda: _gateway(_on_message), name="my-gateway")
```

**Guard for `None`.** The server populates these *before any surface starts*, which means
they are unset at import time and in host-free tests — a route that assumes `host.invoke`
exists will work in production and crash in your test suite.

Every service, with its signature:
[registry.host reference](/reference/plugin-registry-api#host-services-registry-host).

### Tapping core deeper — `graph.sdk` (ADR 0043) {#consumption-sdk}

Use `graph.sdk` for model calls, subagents, knowledge, watches, background jobs,
scheduling, and metrics. It is the supported interface to core capabilities.

```python
from graph import sdk  # import inside the caller when tests need to run host-free

answer = await sdk.complete("Summarize the changelog")
db = sdk.plugin_store(plugin_id=registry.plugin_id) / "state.db"
```

The [SDK reference](/reference/plugin-sdk-api) lists signatures and failure behavior.
For a workflow runner example, see `plugins/workflows`, which injects `run_subagent`
as its per-step runner.

## Events — the plugin bus (ADR 0039) {#event-bus}

Plugins coordinate by **broadcasting events**, never by importing each other. You publish under your
own namespace; interested consumers subscribe by topic. For a direct optional call,
use a registered [plugin service](/reference/plugin-sdk-api#sdk-service) and handle
an absent provider. Neither path imports another plugin’s internals.

```python
def register(registry):
    registry.emit("created", {"id": "a1"})    # → publishes "<plugin_id>.created"
    registry.on("notes.*", on_notes)          # subscribe to ANY topic; * / # wildcards
```

- **Publish is namespace-guarded** — `emit("created")` becomes `<plugin_id>.created`; you can only
  publish under your own namespace. **Subscribing is read-only** and may match any topic.
- **Declare your contract** in the manifest (`emits:` / `subscribes:`) — your events are your public
  API, discoverable in `/api/runtime/status`.
- **Type your contract** (optional) — an `emits:` entry may declare the payload shape so a
  cross-plugin consumer doesn't reverse-engineer your source. See
  [Typed event contracts](#typed-event-contracts) below.
- A console **view** (sandboxed iframe) talks to the bus over the bridge — see
  [Building a plugin view](/guides/building-react-plugin-views). Any event under `<plugin_id>.*` lights your plugin's
  rail icon (a **notification dot**) until the user opens that surface.
- Fire-and-forget + topic-filtered + exception-isolated: a slow or broken subscriber can't affect the
  publisher or other subscribers. Ephemeral (a ring buffer covers SSE reconnects; no durable log).
- The most common subscriber is "when X happens, have the **agent** react" — that composition
  (`on` → prompt-from-payload → `run_in_session`, with an idempotent job id and burst debouncing)
  ships as one consumption-SDK call: `sdk.react_on(…)` ([above](#consumption-sdk)).

> Cross-process note: under the **ACP runtime**, a tool runs in the operator-MCP process where the
> bus isn't wired, so `emit` from a tool won't reach the server bus there. Under the default runtime
> (tool runs in-server) it does.

### Typed event contracts

A names-only `emits:` list tells a consumer *that* a topic exists, not what the payload looks
like — the consumer reverse-engineers the emitter and silently breaks when a field changes. An
`emits:` entry may therefore declare its **payload shape** (#1636): a mapping with `topic` plus an
optional `summary` and/or `schema` (JSON Schema — inline, or a `$ref` to a file inside the plugin
repo, resolved relative to the plugin directory and read at load):

```yaml
emits:
  - spacetraders.window_closed              # bare topic name — still fine
  - topic: spacetraders.trade_executed
    summary: A hauler completed a buy→sell leg
    schema:
      type: object
      required: [route, profit]
      properties: { route: {type: string}, profit: {type: integer}, ship: {type: string} }
  - topic: spacetraders.ship_purchased
    schema: { $ref: events/ship_purchased.json }   # file in the plugin repo
```

- **Purely declarative** (like `capabilities`): the declared shapes ride `/api/runtime/status` as a
  per-plugin `emits_schemas` map (`topic → {summary?, schema?}`), so consumers and the console can
  discover payload shapes. Nothing validates payloads at publish time. (A dev-channel *warn on
  mismatch* validator is a possible later step, gated by a developer flag — deliberately not built
  yet.)
- **Backward compatible**: bare-string entries keep working unchanged, and `emits` stays the
  names-only topic list everywhere it's already consumed.
- **Never load-bearing**: a missing/invalid `$ref`, a ref that escapes the plugin directory, or a
  malformed `schema` logs a warning and degrades that entry to names-only — it never fails the
  plugin load.

## Performance — keep the burden in your plugin

The core console is deliberately lean: one push-based SSE connection, no always-on polling (its
react-query refetches pause when the window is backgrounded). A plugin should be just as
well-behaved — the *only* extra cost should be the one your plugin chooses to add, and it should
go quiet when nobody's looking. This matters doubly for the desktop build.

- **Prefer events over polling.** Subscribe to the bus (`registry.on` / `protoagent:event`) instead
  of polling an endpoint on a timer where you can.
- **If you must poll, pause when hidden.** In a served view, guard the loop with the Page Visibility
  API and refresh on return — don't poll a minimized window:
  ```js
  setInterval(() => { if (!document.hidden) refresh(); }, 1500);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  ```
- **Clean up on unmount.** The console unmounts a plugin view's iframe the moment you tab/collapse
  away — your in-iframe timers/listeners die with it for free. For host-side work (a `registry.on`
  handler, a background surface), return/register a teardown so nothing lingers.

## Persisting state {#persist-state}

Config is for what the *operator* sets. For state your plugin *owns* — a SQLite file, a
cache, exports, generated assets — ask the SDK for a directory:

```python
from graph import sdk

db_path = sdk.plugin_store(plugin_id=registry.plugin_id) / "state.db"
```

That directory is created for you and scoped to the running instance, so the dev sandbox
and every fleet member get their own copy without your plugin doing anything. Pass a
`subdir` (`sdk.plugin_store("exports", plugin_id=…)`) to nest; a path that would escape
your own store is refused rather than silently redirected.

**Don't open core's databases.** `checkpoints.db`, `knowledge.db`, the telemetry and tasks
stores carry no compatibility promise for outside readers, and core migrates them freely.
Reach core data through the [SDK](/reference/plugin-sdk-api) instead.

**Check whether you need a file at all.** These already exist, and each replaces a store
plugins used to hand-roll:

| You want | Use |
| --- | --- |
| A small numeric series (treasury, fleet size, a sparkline, drawdown math) | `sdk.record_metric` / `metric_history` / `metric_last` |
| Facts the agent should be able to retrieve | `sdk.knowledge_add` / `knowledge_search` |
| A whole alternate knowledge engine (pgvector, Qdrant) | `register_knowledge_store` |
| A durable "the operator wanted this running" flag | a small JSON file — see [surfaces that resume](#surface-resume) |
| An index you can rebuild at load | keep it in memory (`sqlite3.connect(":memory:")`) |

If you do open SQLite, the convention that has survived contact with Windows CI is:
connection **per call** (never one shared across threads), `PRAGMA busy_timeout` **before**
`journal_mode=WAL` (the WAL transition itself takes locks), an in-process lock around
writes (`busy_timeout` is a retry loop, not a queue), and additive `ALTER TABLE` migrations
at connect so an operator who has been using your plugin for weeks doesn't lose their rows
to an upgrade. `observability/metrics_store.py` is the reference implementation.

## Config, secrets & settings (ADR 0019) {#config-secrets-settings}

A configurable plugin **declares its config in the manifest** (data, so it's known
at config-load time before `register()` imports). It claims a top-level config
section (default: the plugin id) and gets a Settings group + secrets routing —
no `config.py` / `settings_schema.py` edit:

```yaml
# protoagent.plugin.yaml
config_section: hello          # top-level YAML section (default: the id)
config: { greeting: "Hello", api_key: "" }   # defaults
secrets: [api_key]             # → secrets.yaml (redacted in the UI)
settings:                      # System → Settings group (named after the section)
  - { key: greeting, label: "Greeting word", type: string }
  - { key: api_key,  label: "API key",       type: secret }
```

**Field types:** `string` · `text` (multiline string — a system prompt / template) ·
`number` · `bool` · `select` (with `options: [...]`) · `string_list` · `secret` ·
`path` (a local path on the agent's machine, with a **Browse…** picker over the server's
filesystem; `path_kind: dir` (default) ends on a folder, `path_kind: file` on a file).

**Several paths in one setting — `multiple: true`:** a `path` setting that holds more
than one path (data folders, vaults, repos) adds `multiple: true` and renders as a list:
one row per path, each with its own Browse…, a Remove (×) button, and an **Add folder**
button that appends a row and opens Browse… for it. The stored value is still **one
string** — the rows joined with `\n`, duplicates dropped — so parse it by splitting on
newlines (and commas, which is how an existing comma-separated value loads into rows):

```yaml
settings:
  - { key: data_dirs, label: "Data folders", type: path, path_kind: dir, multiple: true }
```

```python
dirs = [p.strip() for p in re.split(r"[,\n]", cfg.get("data_dirs") or "") if p.strip()]
```

`multiple` is additive: a core that predates it ignores the key and shows the single
path box, which edits the same string — so it needs no `min_protoagent_version` bump.

**Tabbed Configure dialogs:** a larger plugin can declare an ordered tab registry,
then assign ordinary schema-backed fields by stable tab id. Labels are presentation;
ids are identity. Fields without `tab` remain in an implicit **Configuration** tab,
and a plugin without `settings_tabs` keeps the original single flat form.

```yaml
settings_tabs:
  - { id: runtime, label: Runtime }
  - { id: review, label: "Review & merge" }

settings:
  - { key: coder, label: "Coding agent", type: string, tab: runtime }
  - { key: max_concurrent, label: "Concurrent tasks", type: number, tab: runtime }
  - { key: auto_merge, label: "Auto merge", type: bool, tab: review }
  - { key: webhook_secret, label: "Webhook secret", type: secret } # Configuration
```

Tab ids must match `[A-Za-z0-9][A-Za-z0-9_-]*` and be unique within the
manifest. An invalid descriptor or an unknown field reference is ignored with a
warning; the affected field falls back to Configuration rather than disappearing.
Tab switching retains one shared dirty state, so **Save & apply** submits pending
changes from every tab together.

**Conditional fields** — add `depends_on` to show a field only once a sibling is set
(e.g. an "enable X" toggle gates X's options); reactive to the in-form value:

```yaml
settings:
  - { key: ask_enabled, label: "Interactive", type: bool }
  - { key: ask_system,  label: "Ask system instruction", type: text,
      depends_on: { key: ask_enabled, equals: true } }   # also: { key, in: [...] } | bare { key } = truthy
```

**Required config & incomplete plugins (#1719):** mark a setting `required: true`
to declare the plugin needs it to work. If an enabled plugin loads while a required
field is still blank, it **stays loaded but is flagged `incomplete`** — a soft gate,
not `requires_env` (which refuses to load). `GET /api/runtime/status` and
`/api/plugins/installed` then carry `incomplete: true` + `needs_config: [{key, label}]`,
and the plugin's **tools are swapped for same-signature stand-ins that return a
friendly "needs setup" notice** instead of erroring mid-call — so the agent can point
the operator at configuration. Fill the field in; the next config reload restores the
real tools. (`0` / `false` count as provided — only `null` / empty-string / empty-list
read as "unset".)

```yaml
settings:
  - { key: api_key, label: "API key", type: secret, required: true }
```

**Settings that name a program to spawn** — mark them `spawns: true`. An agent with
the guarded `set_config` tool (`tools.self_config_enabled`) may retune your plugin's
settings, but must never be able to point one at a binary your plugin then runs. The
fence already refuses keys whose *name* says so — any `_`/`-`/camelCase token of
`command` `cmd` `args` `argv` `binary` `bin` `exe` `executable` `interpreter`
`entrypoint` (`local_gate_cmd`, `rh_bin`, `binary_path`) — so prefer such a name. When
the natural name doesn't (`ffmpeg_path`), declare it:

```yaml
settings:
  - { key: ffmpeg_path, label: "ffmpeg path", type: string, spawns: true }
```

Only the agent's writer is fenced; the operator still sets the key from Settings. Older
cores ignore the field, so declaring it never breaks a load. See
[ADR 0019 §3b](/adr/0019-plugin-config-settings-secrets).

Read the resolved config (manifest defaults ⊕ YAML ⊕ secrets) in `register()`:

```python
def register(registry):
    greeting = registry.config.get("greeting", "Hello")  # ADR 0019
    registry.register_router(_build_router(greeting))    # close over it
```

A plugin section colliding with a reserved built-in (`model`, `mcp`, `plugins`,
…) is ignored. (A plugin section like `discord` is **not** reserved — a plugin,
bundled or external, claims its own section the same way.)
A plugin declares its required config with `required: true` (above) and the console
surfaces the **incomplete** state so an operator knows to finish setup; a guided
install **wizard** over those fields is the frontend follow-up (#1719).

**Setup gaps the config can't see — `registry.report_setup_gap(key, message)`.** A
required setting catches a *blank field*; it can't catch a missing binary on PATH, a
coder delegate the member doesn't have, or a CLI that isn't logged in. For those a
plugin reports the gap itself and the console shows it as an operator **warning
banner**. `GET /api/runtime/status` carries it as a structured record in `setup_gaps[]`
(and as the legacy `"<Plugin>: <message>"` line in `warnings[]`); the console renders the
record as a dismissible banner, with a **Configure** button that opens your plugin's
settings when you pass `action={"kind": "plugin_config"}` (see
[`report_setup_gap()`](../reference/plugin-registry-api.md#registry-report-setup-gap) for
the closed action vocabulary). Pass `message=None` to clear it — re-check on each tick or request and the banner
self-heals the moment the operator installs the binary / adds the delegate, no
restart. One key per concern (`"br"`, `"coder"`, `"auth"`); a disabled plugin's gaps
are dropped on the next reload. Guard it for hosts that predate the seam:

```python
def _preflight(registry):
    fn = getattr(registry, "report_setup_gap", None)
    if not callable(fn):
        return
    fn("br", None if shutil.which("br") else "beads CLI 'br' not on PATH — install beads-rust and restart")
```

**When the fix is a command, give the banner a button that runs it — `plugin_setup`.** A
`plugin_config` action can only open a settings form; "download the CLI" or "install the
browser" would still send the operator to a terminal. Register the command as a **setup
step** with `registry.register_setup_step(step, fn)` and report the gap with
`action={"kind": "plugin_setup", "step": step, "label": "Download the CLI"}`: the banner
renders a button, a click POSTs `/api/plugin-setup/<id>/<step>`, and the host runs the
`fn` it holds for exactly that plugin and step — the action is only the step's name. The
route is core and sits outside `/api/plugins/<id>/` on purpose: your manifest's
`public_paths` / `federation_paths` can open that subtree, and a setup step always takes the
operator credential. If the plugin is disabled, uninstalled, or fails to reload, its steps
go with its gaps, and nothing the old load reports afterwards brings a banner back. Start long work on a thread, re-report the gap with its progress
and no action (so it can't be clicked twice), and return
`{"ok": True, "pending": True, "message": …}`; the console keeps refreshing the banner
while the step runs, and your re-report on completion clears it or shows the error with a
Retry. [`register_setup_step()`](../reference/plugin-registry-api.md#registry-register-setup-step)
has the contract; the bundled `agent_browser` plugin's Download agent-browser and Install
Chrome buttons are the worked example.

**Routes and surfaces hot-reload.** On a config reload a newly-enabled
plugin's **routers, public paths, verifiers, hooks, tools, subagents, chat commands,
and MCP servers re-apply** without a restart (#1752/#1890). Surfaces are reconciled: a
disabled plugin's surface stops, a newly-enabled one starts, and a surviving surface gets
its `reload(cfg)` callback when both its old and new registration declare one.
`register()` re-runs on every reload, so a surviving surface whose new registration
lacks a `reload` hook and hands back a different `stop` (fresh closures over a fresh
dispatcher/queue) is **stopped, then started from the new registration** — otherwise the
old loop would keep driving the old objects while the new routes use the new ones
(#3593). The old task gets a grace period, then a cancel; one that still won't end is
kept, and its replacement is not started. A disabled or uninstalled plugin's surface gets the
same grace and cancel. A surface that outlives all of it is the one plugin contribution that
needs a process restart, and the console's install / update / toggle / uninstall response says
so (`restart_recommended`). Declare `reload` to keep a surface running across saves,
or register module-level/singleton `start`/`stop` so a re-register is the same surface. Everything is best-effort: a failing
plugin/route/surface logs and never breaks boot. The shipped [`plugins/hello`](https://github.com/protoLabsAI/protoAgent/tree/main/plugins/hello)
example demonstrates the contribution types. Plugin contributions show in
`GET /api/runtime/status`. The bundled `plugins/telegram` (the reference
`ChatAdapter`) and `plugins/friction` first-party plugins are worked examples of the
contribution types; the external `discord-plugin` and `github-plugin` are fuller
surface + route + tools (+ status probe) examples.

## Where plugins live & how they're enabled {#enable-one}

Bundled plugins live in the shipped `plugins/` tree. Installed plugins and local
drop-ins live in `<instance_root>/plugins/`; `plugins.dir` or
`PROTOAGENT_PLUGINS_DIR` can override that directory. Run `protoagent config explain`
to find it. A live plugin overrides a bundled plugin with the same `id`.

Directory overrides must be absolute; invalid relative values are ignored with a
warning. A plugin must be enabled in `plugins.enabled` or its manifest to load.
Discovered but disabled plugins still appear in runtime status.

Use [Install and manage plugins](/guides/plugin-registry) for enabling, disabling,
updating, and recovering installations. Changes normally reload tools, routes,
views, and background surfaces immediately; restart only when the app requests it.
Plugin tools that shadow core or MCP names are skipped. Bundled skills are
re-seeded as disk-source skills at boot.

## Keeping plugins current

For manual updates and opt-in automatic update policies, see
[Manage plugin updates](/guides/plugin-registry#automatic-updates).

## Behavior

- Loading is **best-effort**: a broken plugin (bad manifest, import error,
  missing `requires_env`) is logged and skipped — it never blocks boot.
- `GET /api/runtime/status` lists `plugins` with `{id, name, enabled, loaded,
  tools, skills}`.
- Plugins are (re)loaded at startup and on config reload.

## Test it host-free (the testkit)

`src/graph/plugins/testkit.py` is a host-free test harness: it loads a plugin the way the
runtime does (as a package, so relative imports and deep engine modules work), stubs the
host-only `graph.*` / `knowledge.*` imports, and hands `register()` a **`FakeRegistry`**
that captures every contribution — so a plugin's real modules run under plain `pytest`
with no protoAgent server. It's stdlib-only by design: `scaffold_plugin(with_tests=True)`
vendors it verbatim into a standalone plugin repo as `tests/_plugin_testkit.py`; bundled
plugins import it directly (`from graph.plugins.testkit import load_plugin,
install_host_stubs, FakeRegistry`).

```python
install_host_stubs()                       # graph.* / knowledge.* resolve with no host
pkg = load_plugin("path/to/my-plugin")     # loaded as a package, like the runtime
reg = FakeRegistry()
pkg.register(reg)
assert reg.tools and "issue" in reg.chat_commands   # assert the captured contributions
```

The fake mirrors the registry's public methods. Invalid registrations raise
`ValueError` in tests even where the live registry warns and skips them. See the
[testkit reference](/reference/plugin-testkit) for the complete test API.

## Try it

Enable the shipped example:

```yaml
plugins:
  enabled: [hello]
```

Restart, then check `GET /api/runtime/status` — the `hello` plugin shows
`loaded: true` with its `hello` tool and `greeting` skill.

## Related

- **[Building a plugin view](/guides/building-react-plugin-views)** — give a plugin its own
  console surface — a left-rail view or a chat-slot panel (ADR 0026 / 0045).
- [Install and manage plugins](/guides/plugin-registry) — operator controls and updates.
- [Publish a plugin](/guides/publish-a-plugin) — dependencies, packaging, and listing.
