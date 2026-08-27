# Plugins

Plugins are **drop-in packages** that extend protoAgent without forking it. A
plugin contributes **tools**, bundled **skills**, FastAPI **routes**, background
**surfaces**, **subagents**, **middleware**, knowledge backends/embedders, goal
verifiers — plus its own **config / secrets / Settings** (ADR 0018/0019/0032).
Plugins run **in-process** with the agent's privileges, so they're **disabled by
default** and you opt in explicitly — only enable plugins you trust.

> The first-party **Telegram** integration ships bundled as a plugin
> (`plugins/telegram/`), opt-in via `plugins: { enabled: [telegram] }`.
> Integrations like **Discord**, **GitHub**, **Google** Gmail/Calendar, and **Slack** install as
> **external** plugins from their own repos (browse + install them in Settings ▸
> Plugins ▸ Discover). To drive a **CLI coding agent over ACP**, enable the **delegates**
> plugin and declare an `acp` delegate — see
> [CLI coding agents over ACP](/guides/coding-agents).

> **Trust model.** This is the in-process / trusted model (matching Hermes): an
> enabled plugin's `register()` runs as the agent. Don't enable code you
> haven't reviewed. Untrusted third-party *tools* are better added via
> [MCP](./mcp.md) (out-of-process).

## Start here

This page explains how to *build* one. When you already know the shape and just need a
signature, a field, or a flag, go to the reference instead — it is generated from the code,
so it is complete and current by construction:

| Reference | Contents |
| --- | --- |
| [Plugin manifest](/reference/plugin-manifest) | Every `protoagent.plugin.yaml` field — type, default, meaning |
| [Registry API](/reference/plugin-registry-api) | Every `register_*` seam with its exact signature, plus `registry.host` |
| [Plugin SDK](/reference/plugin-sdk-api) | `graph.sdk` — subagents, knowledge, watches, background jobs, metrics |
| [Testkit](/reference/plugin-testkit) | `load_plugin`, `FakeRegistry`, host stubs |
| [Plugin CLI](/reference/plugin-cli) | `python -m server plugin …` |
| [View bridge](/reference/plugin-view-bridge) | The sandboxed-iframe `postMessage` protocol |
| [Event bus topics](/reference/plugin-events) | Every topic core publishes, with payload keys |

New to plugins? [Build your first plugin](/tutorials/first-plugin) is the twenty-minute version,
and [Plugin architecture](/explanation/plugin-architecture) explains the model — what runs
in-process, what's sandboxed, and when your code is called.

Otherwise, pick the job you actually have:

| I want to… | Go to |
| --- | --- |
| **Turn on a plugin that ships with protoAgent** | Settings ▸ Plugins ▸ Installed, or `plugins: { enabled: [telegram] }` — [Enable one](#enable-one) |
| **Install someone else's plugin from a git URL** | [Install & publish plugins](/guides/plugin-registry) |
| **Write my own** | [Anatomy](#anatomy) — the manifest, `register()`, and what you can contribute (tools, routes, a console view, …) |
| **Add a console surface to one** | [Building a plugin view](/guides/building-react-plugin-views) |
| **Turn my agent into a chat bot** | [Build a communication plugin](/guides/communication-plugins) |

The rest of this page walks the plugin contract top to bottom — read it once when you
write your first plugin, then use the [reference pages](/reference/) for lookup.

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
id: hello                 # required, unique
name: Hello Plugin        # required
version: 0.1.0
description: One-line summary.
enabled: false            # author opt-in; operators can also enable by id in config
entrypoint: ""            # optional module filename (defaults to __init__.py / plugin.py)
requires_env: []          # env vars the plugin needs (missing → skipped + logged)
capabilities:             # declarative, for transparency (not yet enforced)
  network: []
  filesystem: none
emits: []                 # event-bus topics this plugin broadcasts (ADR 0039) — its public API.
                          # An entry is a bare topic name, or {topic, summary?, schema?} to
                          # declare the payload shape — see "Typed event contracts" below
subscribes: []            # topics it listens for (declarative — for discoverability)
public_paths: []          # auth-exempt prefixes under THIS plugin's own namespace
                          # (/plugins/<id>/… or /api/plugins/<id>/…) — for an inbound
                          # webhook (no bearer; you verify its signature). See below.
federation_paths: []      # prefixes under THIS plugin's own namespace that accept the
                          # federation credential (ADR 0066) — still authenticated, only
                          # the /api operator ceiling is lowered. See below.
```

**Every field, with its type and default: [Plugin manifest reference](/reference/plugin-manifest).** Generated from the dataclass the loader reads, so it can't fall behind. The fields worth explaining rather than listing have their own sections below.

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

`register` is called once at load. The registry accepts these contribution types
(plus console **views**, declared in the manifest — see [Building a plugin view](/guides/building-react-plugin-views)) —
a fork adds any of them as a plugin, never editing the core `server/` package:

| Method | Contributes | Lifecycle |
|---|---|---|
| `register_tool(tool)` / `register_tools(iter)` | A LangChain tool | graph build (live-reloads) |
| `emit(topic, data)` / `on(topic, handler)` | Broadcast / subscribe on the **event bus** (ADR 0039) — `emit` auto-namespaces to `<plugin>.<topic>`; `on` takes `*`/`#` wildcards | any time (publish is fire-and-forget) |
| `navigate(view="")` | Ask the console to focus one of **this plugin's own views** (ADR 0044) — scoped to your plugin id, blank opens the first view | any time (fire-and-forget) |
| `live_config()` | The plugin's **current** resolved config, re-read from the host each call — use it inside a mounted router/surface so config edits take effect without a restart (`registry.config` is a register-time snapshot) | any time |
| `register_skill_dir(path)` | A `SKILL.md` directory (procedural memory) | graph build |
| `register_workflow_dir(path)` | A directory of `*.yaml` workflow recipes | workflow-registry build |
| `register_a2a_skill(spec)` | An A2A **card** skill (what the card advertises; optional structured output) | agent-card build |
| `register_router(router, prefix=None)` | A FastAPI `APIRouter` | **mounted once** at init (default prefix `/plugins/<id>`) |
| `register_surface(start, stop=None, name=None, reload=None)` | A background surface (a Discord-style gateway) | `start` in startup, `stop` in shutdown, `reload(cfg)` on config save |
| `register_subagent(config)` | A `SubagentConfig` (a delegate) | added to `SUBAGENT_REGISTRY` |
| `register_middleware(factory)` | A LangGraph **`AgentMiddleware`** (per-turn before/after-model + tool hooks) — `factory(config) → middleware \| None` | graph build; appended before message-capture (ADR 0032) |
| `register_goal_verifier(name, fn)` | An in-process **goal/watch verifier** (ADR 0028) — dispatched by a `{"type": "plugin", "check": "<plugin-id>:<name>"}` goal or watch spec | graph build (re-set on reload) |
| `register_goal_hook(on_achieved=, on_failed=)` | React when a **goal** reaches a terminal state (ADR 0028) — the `GoalState` in, push a notification / set the next goal | graph build (re-set on reload) |
| `register_watch_hook(on_met=, on_expired=, on_stalled=)` | React when a **watch** trips (ADR 0067) — met / deadline passed / evidence stalled | graph build (re-set on reload) |
| `register_lifecycle_hook(on_app_loaded=, on_agent_active=, on_system_wake=)` | React to a **system lifecycle** event (ADR 0074) — see [Lifecycle events](/guides/lifecycle-events) | graph build (re-set on reload) |
| `register_knowledge_store(name, factory)` | A **knowledge backend** (ADR 0031) — `factory(config) → KnowledgeBackend`, selected by a fork's `knowledge.backend: "<name>"` (pgvector/Qdrant/…); degrades to the built-in SQLite store on error | graph build |
| `register_embedder(name, factory)` | An in-process **embedder** (ADR 0031) — `factory(config) → (text) → vector`, selected by `knowledge.embedder: "<name>"` to skip the gateway round-trip; degrades to the gateway embedder on error | graph build |
| `register_mcp_server(factory)` | A **managed MCP server** the agent connects to | `factory(config)` called at each graph build → entry dict or `None` |
| `register_thread_id_resolver(fn)` | A `(request_metadata, session_id) → str` checkpointer-scope resolver (e.g. per-project memory) | each turn; one wins (last plugin) |
| `register_chat_command(name, handler)` | A **user-only** `/<name>` chat control command that short-circuits the turn (the generalized `/goal`) — token slugified+lowercased; `goal` and `lifecycle` are reserved core tokens (refused); see [publish guide](/guides/plugin-registry) | chat dispatch; first plugin to claim a token wins |
| `register_late_tool_factory(factory)` | A tool factory that runs **after** the full toolset is assembled — `factory(all_tools, config) → tool \| list \| None`, for meta-tools that must see every other tool | graph build, appended last |
| `save_media(data, mime, meta=None)` | Persist a generated binary artifact (image/audio/video) into the **core media store** (#1929) → `MediaRef {id, url, path, mime}`. Embed `ref.url` in the tool's returned markdown (`![alt](url)`) and the console renders it inline — no plugin route needed. The URL carries a per-file HMAC signature so it works under a bearer gate; `media.public` / `media.retention_days` (core config) control exposure and pruning; broadcasts `media.saved` on the bus. Inbound is bridged too (#1969): chat image attachments are auto-saved at turn entry (except incognito) and named by media id in a `[attached-image refs]` note, so a tool that accepts media-id refs works on user attachments directly | any time (typically inside a tool) |

```python
def register(registry):
    registry.register_tool(hello)
    registry.register_a2a_skill({"id": "greet", "name": "Greet", "description": "..."})
    registry.register_router(_build_router())        # → GET /plugins/<id>/...
    registry.register_surface(_start, stop=_stop, name="my-surface")
    registry.register_subagent(_build_subagent())    # delegate via task/task_batch
    registry.register_mcp_server(_server_factory)    # a managed MCP server (e.g. an OAuth-gated surface)
    registry.register_thread_id_resolver(lambda md, sid: f"proj:{md.get('project')}:{sid}")
```

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
   per-agent config dir (`PROTOAGENT_CONFIG_DIR`), the same store your watch/plan state uses.
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

### Middleware — `register_middleware` (ADR 0032)

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

`registry.host` covers the common cases. For deeper capability, import the **consumption
SDK** — a stable surface onto the agent itself, so a plugin can run a subagent, search
knowledge, arm a watch, spawn campaign-scale background work, schedule a recurring turn, or
record a metric series without reaching into core internals (which are free to change
underneath you — `graph.sdk` and `registry.host` are not).

```python
from graph import sdk                      # import lazily if your tests run host-free

answer = await sdk.complete("Summarize the changelog")
db = sdk.plugin_store(plugin_id=registry.plugin_id) / "state.db"
```

**Every call, with its signature and failure mode: [Plugin SDK reference](/reference/plugin-sdk-api).**
It is generated from the source, so it is complete and current — this guide deliberately
doesn't restate it.

Two things the reference can't tell you. First, **import it inside the function that uses
it** if your plugin must also run host-free in its own test suite; a module-level
`from graph import sdk` makes the whole plugin need a host to import. Second, the
**workflows plugin** (`plugins/workflows`) is the reference consumer — its engine injects
`run_subagent` as the per-step runner, which is the pattern to copy for a plugin that drives
the agent rather than just extending it.

The SDK also re-exports a few host-free kits so a plugin doesn't hand-roll them:
`supervise` / `Supervisor` / `RetryAfter` (a watchdog-backed runner for a self-perpetuating
engine loop), `Knobs` / `make_knob_tools` (bounded, reversible tunables with generated
`show`/`tune`/`preset` tools), and `DecisionLog` / `telemetry` / `render_html` (an audit
trail plus a themed panel).

## Events — the plugin bus (ADR 0039) {#event-bus}

Plugins coordinate by **broadcasting events**, never by importing each other. You publish under your
own namespace and forget; anyone who cares subscribes by topic. This is the only inter-plugin
channel — the **no-cross-dependency** rule.

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
`number` · `bool` · `select` (with `options: [...]`) · `string_list` · `secret`.

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
banner** (`GET /api/runtime/status` → `warnings[]`, rendered as `"<Plugin>: <message>"`).
Pass `message=None` to clear it — re-check on each tick or request and the banner
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

**Routes now hot-reload; surfaces still don't.** On a config reload a newly-enabled
plugin's **routers, public paths, verifiers, hooks, tools, subagents, chat commands,
and MCP servers re-apply** without a restart (#1752/#1890). A **surface** does not — the
startup hook already fired, so it (re)starts only on a full restart; a config reload just
calls each running surface's `reload(cfg)` callback. Everything is best-effort: a failing
plugin/route/surface logs and never breaks boot. The shipped [`plugins/hello`](https://github.com/protoLabsAI/protoAgent/tree/main/plugins/hello)
example demonstrates the contribution types. Plugin contributions show in
`GET /api/runtime/status`. The bundled `plugins/telegram` (the reference
`ChatAdapter`) and `plugins/friction` first-party plugins are worked examples of the
contribution types; the external `discord-plugin` and `github-plugin` are fuller
surface + route + tools (+ status probe) examples.

## Where plugins live & how they're enabled {#enable-one}

Two roots (like skills): bundled `plugins/` (shipped, e.g. the `hello` example)
and live `<config-dir>/plugins/` (your drop-ins; `<config-dir>` honors
`PROTOAGENT_CONFIG_DIR`, override with `plugins.dir`). Live overrides bundled by `id`.

A plugin loads only when **enabled** — either:

```yaml
plugins:
  enabled: [hello]   # operator opt-in, by id
```

or `enabled: true` in the plugin's own manifest (author opt-in for plugins you
wrote/dropped in). Discovered-but-disabled plugins still appear in runtime
status so you can see what's available.

From the console, the **Plugins ▸ Installed** panel is a sortable table: free-text search
matches plugin names, ids, versions, **and tool names** ("which plugin ships `search_jobs`?"),
status chips filter to All / Loaded / Disabled / **Attention** (error, unfinished setup,
update available, or missing pip deps), and plugins installed by a bundle carry the bundle's
name as a chip (searchable too). Each row has a one-click **Enable / Disable** toggle — it
edits `plugins.enabled` and hot-reloads, so tools / middleware / MCP servers apply
immediately. A plugin that serves a **console view** or runs a **background surface**
(its router mounts at startup) needs a restart to finish — the toggle says so.

Plugin tools that would shadow a core or MCP tool name are skipped (logged).
Bundled skills load as `disk`-source [skills](./skills.md), re-seeded each boot.

## Keeping plugins current

A git-URL-installed plugin is pinned in `plugins.lock` at the ref you installed
(a branch, a release tag, or an exact commit). By default updates are **manual**:
the console **Plugins** panel shows an *Update available* badge when a plugin is
behind its ref, and the **Update** button pulls the latest code + hot-reloads it
(`POST /api/plugins/{id}/update`).

To let chosen plugins update themselves in the background, opt them into an
**auto-update policy** (#1720):

```yaml
plugins:
  autoupdate_interval_hours: 6      # sweep cadence; 0 disables the loop entirely
  update_policy:
    my-plugin:
      track: main                   # arms auto-update (the ref itself comes from the lock)
      when: idle                    # idle (default) | always
    another-plugin:
      track: main
      when: always
```

Each sweep, for every plugin listed in `update_policy`, the runtime checks whether
it's behind its locked ref and — if so — pulls + hot-reloads it exactly like the
**Update** button, then emits `plugin.updated` on the [event bus](#event-bus).
The gates:

- **Opt-in only.** A plugin is auto-updated only if it appears in `update_policy`
  with a non-empty `track`. Everything else stays manual.
- **Never a pinned commit.** A plugin pinned to an exact SHA is immutable and is
  skipped; a release-tag pin moves to the newest matching tag, a branch pulls its head.
- **`when: idle`** (the default) defers a plugin's update while a chat turn is — or
  was just — in flight. A reload rebuilds tools/routers, which is safe *between*
  turns but disruptive *during* one. **`when: always`** updates on the next sweep
  regardless.

The default config has an empty `update_policy`, so nothing auto-updates until you
add a plugin to it.

## Behavior

- Loading is **best-effort**: a broken plugin (bad manifest, import error,
  missing `requires_env`) is logged and skipped — it never blocks boot.
- `GET /api/runtime/status` lists `plugins` with `{id, name, enabled, loaded,
  tools, skills}`.
- Plugins are (re)loaded at startup and on config reload.

## Test it host-free (the testkit)

`graph/plugins/testkit.py` is a host-free test harness: it loads a plugin the way the
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

**The parity contract:** `FakeRegistry` mirrors every public `PluginRegistry` method
(`register_*`, `emit`, `on`, `navigate`, `live_config`) with the same parameters — a
missing method would make that seam silently untestable (plugins `hasattr`-guard these
calls, so a typo'd registration would ship green). A drift guard in
`tests/test_plugin_testkit.py` introspects both classes and fails when a new registry
seam isn't mirrored. One intentional divergence: where the real registry *warns and
skips* an invalid registration (degrade-safe live — e.g. a chat command named `goal`,
which is reserved), the fake **raises `ValueError`** so the mistake fails your test.

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
- **[Install & publish plugins (git URLs)](/guides/plugin-registry)** — install a
  plugin from a git URL (`python -m server plugin install <url>`) or publish one as
  a shareable repo. A repo is a full bundle: besides what `register()` adds, a
  conventional `skills/` (SKILL.md) and `workflows/` (`*.yaml`) are auto-discovered
  (ADR 0027).
