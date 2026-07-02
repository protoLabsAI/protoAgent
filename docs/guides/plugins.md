# Plugins

Plugins are **drop-in packages** that extend protoAgent without forking it. A
plugin contributes **tools**, bundled **skills**, FastAPI **routes**, background
**surfaces**, **subagents**, **middleware**, knowledge backends/embedders, goal
verifiers — plus its own **config / secrets / Settings** (ADR 0018/0019/0032).
Plugins run **in-process** with the agent's privileges, so they're **disabled by
default** and you opt in explicitly — only enable plugins you trust.

> The first-party **Telegram** and **GitHub** integrations ship bundled as plugins
> (`plugins/telegram/`, `plugins/github/`), opt-in via `plugins: { enabled: [telegram] }`.
> Integrations like **Discord**, **Google** Gmail/Calendar, and **Slack** install as
> **external** plugins from their own repos (browse + install them in Settings ▸
> Plugins ▸ Discover). To drive a **CLI coding agent over ACP**, enable the **delegates**
> plugin and declare an `acp` delegate — see
> [CLI coding agents over ACP](/guides/coding-agents).

> **Trust model.** This is the in-process / trusted model (matching Hermes): an
> enabled plugin's `register()` runs as the agent. Don't enable code you
> haven't reviewed. Untrusted third-party *tools* are better added via
> [MCP](./mcp.md) (out-of-process).

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
requires_env: []          # env vars the plugin needs (missing → skipped + logged)
capabilities:             # declarative, for transparency (not yet enforced)
  network: []
  filesystem: none
emits: []                 # event-bus topics this plugin broadcasts (ADR 0039) — its public API.
                          # An entry is a bare topic name, or {topic, summary?, schema?} to
                          # declare the payload shape — see "Typed event contracts" below
subscribes: []            # topics it listens for (declarative — for discoverability)
```

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
| `register_skill_dir(path)` | A `SKILL.md` directory (procedural memory) | graph build |
| `register_workflow_dir(path)` | A directory of `*.yaml` workflow recipes | workflow-registry build |
| `register_a2a_skill(spec)` | An A2A **card** skill (what the card advertises; optional structured output) | agent-card build |
| `register_router(router, prefix=None)` | A FastAPI `APIRouter` | **mounted once** at init (default prefix `/plugins/<id>`) |
| `register_surface(start, stop=None, name=None, reload=None)` | A background surface (a Discord-style gateway) | `start` in startup, `stop` in shutdown, `reload(cfg)` on config save |
| `register_subagent(config)` | A `SubagentConfig` (a delegate) | added to `SUBAGENT_REGISTRY` |
| `register_middleware(factory)` | A LangGraph **`AgentMiddleware`** (per-turn before/after-model + tool hooks) — `factory(config) → middleware \| None` | graph build; appended before message-capture (ADR 0032) |
| `register_goal_verifier(name, fn)` | An in-process **goal/watch verifier** (ADR 0028) — dispatched by a `{"type": "plugin", "check": "<plugin-id>:<name>"}` goal or watch spec | graph build (re-set on reload) |
| `register_mcp_server(factory)` | A **managed MCP server** the agent connects to | `factory(config)` called at each graph build → entry dict or `None` |
| `register_thread_id_resolver(fn)` | A `(request_metadata, session_id) → str` checkpointer-scope resolver (e.g. per-project memory) | each turn; one wins (last plugin) |
| `register_chat_command(name, handler)` | A **user-only** `/<name>` chat control command that short-circuits the turn (the generalized `/goal`) — token slugified+lowercased; `goal` reserved; see [publish guide](/guides/plugin-registry) | chat dispatch; first plugin to claim a token wins |
| `register_late_tool_factory(factory)` | A tool factory that runs **after** the full toolset is assembled — `factory(all_tools, config) → tool \| list \| None`, for meta-tools that must see every other tool | graph build, appended last |

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

A surface or route often needs to **call the agent** or the **event bus** — host
services it can't build. `registry.host` exposes them (the server populates them
before any surface starts; guard for `None`):

- `host.invoke(prompt, session_id)` — run a chat turn (one conversation per
  `session_id`), returns the assistant text.
- `host.publish(event, data)` / `host.subscribe()` — the server→client event bus.
- `host.on(topic, handler)` — subscribe an in-process handler to bus topics (ADR 0039); prefer the
  `registry.emit` / `registry.on` wrappers, which namespace + guard for you.
- `host.config()` — the live `LangGraphConfig` (current resolved values, incl.
  `plugin_config`), so a route reads fresh config instead of a load-time snapshot.
- `host.apply_settings(patch)` — persist a nested config patch + reload once
  (heavy — call via `asyncio.to_thread`). Lets a route apply config (e.g. an OAuth
  Connect flow flips `enabled` and reloads).

```python
def register(registry):
    host = registry.host
    async def _on_message(text, sid):
        return await host.invoke(text, sid)        # call the agent
    registry.register_surface(lambda: _gateway(_on_message), name="my-gateway")
```

### Tapping core deeper — `graph.sdk` (ADR 0043) {#consumption-sdk}

`registry.host` covers the common cases. For deeper capability, import the **consumption
SDK** directly — `from graph.sdk import …`, the *stable* surface plugins call into core
(so core can refactor underneath you; never reach into `graph.agent` internals). v1:

- `run_subagent(subagent_type, prompt, *, description)` — run **one subagent** to
  completion (vs `host.invoke`, which runs a full lead-agent *chat turn*).
- `subagent_types()` — the configured subagent ids.
- `config()` — the live `LangGraphConfig`.
- `knowledge_search(query, *, k=5, domain=None, epoch=None)` /
  `knowledge_add(content, *, domain="general", heading=None, epoch=None)` — the
  plugin↔knowledge channel: search the agent's knowledge graph (hybrid FTS5 +
  embeddings) and write chunks back, scoped to a `domain` bucket. Both degrade to a
  no-op (`[]` / `None`) without a store. `epoch` (#1634) tags a chunk with the **era**
  it was learned in (an opaque string — typically a reset date); passing `epoch=` to
  `knowledge_search` filters **both** rankings to exactly that era, so a plugin in a
  resettable world (spacetraders' weekly wipes) retires old lessons by just searching
  with the new tag — they stay stored for post-mortems but stop polluting retrieval.
- `knowledge_purge(domain, *, before=None) -> int` — the knowledge **lifecycle**
  primitive (#1634): hard-delete every chunk in a domain (optionally only those
  created before an ISO-8601 timestamp) and return the count. Deletes consistently
  from every index (rows, FTS, vectors); on a layered store only the **private** tier
  is purged (the commons is curated, never bulk-deleted). Refuses (returns 0) on an
  empty domain or an unparseable `before`. See
  [Knowledge ▸ Plugin knowledge lifecycle](/guides/knowledge#plugin-knowledge-lifecycle).
- `run_in_session(session_id, prompt, *, delay_seconds=0, job_id=None)` — enqueue a
  **non-blocking one-shot agent turn** in a session (that session's memory + full tools).
  The primitive behind "when a goal fires, prompt the agent" — call it from a
  `register_goal_hook` reaction. See [Goal mode ▸ Reacting to a goal](/guides/goal-mode#reacting-to-a-goal).
- `create_watch(*, condition, verifier, run_prompt=…, …)` — register a **watch** (ADR 0067):
  poll `condition` on a cadence, and on met run `run_prompt` as a follow-up turn
  (`run_in_session`) + fire `on_met` hooks. Plugin-verifier only; hold **many** at once (unlike
  a monitor goal). Pair with `registry.register_watch_hook(on_met/on_expired/on_stalled=…)`.
- `list_watches(prefix="")` / `clear_watch(watch_id)` — the watch **lifecycle** half (#1638):
  enumerate the registered watches (each `{id, condition, status, verifier}`, optionally
  id-prefix-filtered — e.g. `list_watches("st-")` for your own suite) and remove one by id
  (`True` if it existed). Together they make a plugin's arm step a *reconcile* — clear the
  suite ids no longer in your spec set, then create/replace the rest — so a renamed/dropped
  watch spec can't leak a zombie watch. See [Watches](/guides/watches).
- `spawn_background(prompt, *, subagent_type, origin_session, label=None)` — spawn a
  **detached background subagent job** ([ADR 0050](/adr/0050-background-subagents-reactive-notifications))
  that returns a `bg-…` id immediately and rides the full
  [ADR 0070](/adr/0070-background-results-push-resume) results pipeline (push-resume nudge
  into `origin_session`, KB-indexed report, console report card). The seam for long
  campaign work — never reach into `STATE.background_mgr` directly.
- `background_status(task_id)` — the status-query companion: `{status, description,
  report?}` for a spawned job (`report` once terminal), so a plugin can render progress
  on its own surface between launch and the completion nudge.
- `react_on(topic, *, prompt, job_id, session=…, debounce_s=0)` — **reactive-rule sugar**:
  when a bus event matching `topic` fires, build a prompt from the payload and enqueue a
  follow-up turn (`run_in_session`). `prompt(event) -> str | None` (`None`/empty skips the
  event), `job_id` makes re-fires replace rather than stack, `debounce_s` coalesces a burst
  into ONE turn (trailing-edge; the last event's prompt wins), `session` defaults to the
  Activity thread. Returns an unsubscribe fn. The one-call form of the canonical
  `registry.on` → `run_in_session` composition — see [Events](#events-the-plugin-bus-adr-0039).
- `schedule_recurring(prompt, cron, *, plugin_id, job_id, session="", timezone=None)` — a
  plugin-owned **recurring** cadence (#1642): a cron job whose id is namespaced
  `plugin:<plugin_id>:<job_id>` so the host cancels it on disable/uninstall (no orphan
  cadence outlives its plugin). Idempotent by id (a re-call replaces), fires into Activity
  by default; pass `plugin_id=registry.plugin_id`. One-shot turns stay on `run_in_session`.
  With `cancel_scheduled(job_id, *, plugin_id)` and `cancel_plugin_jobs(plugin_id)` — see
  [Scheduler ▸ Plugin-owned recurring jobs](/guides/scheduler#plugin-owned-recurring-jobs).
- `record_metric(name, value, *, ts=None, plugin_id)` / `metric_history(name, *,
  since=None, limit=500, plugin_id)` / `metric_last(name, *, plugin_id)` — a plugin
  **metric timeseries** (#1632): small named numeric series (treasury, net worth, fleet
  size), namespaced `<plugin_id>:<name>` into one per-instance SQLite store
  (`metrics.db`), retention-capped per series (90 days / 10k points, trimmed on write —
  record freely from an engine tick). This is the *history* a live-state watch verifier
  (ADR 0067) can't get any other way — drawdown vs high-water mark, flatline detection —
  and the substrate for dashboard sparklines. Timestamps are Unix epoch seconds
  (`ts=None` → now); `metric_history` returns the newest `limit` points (optionally at/after
  `since`) **oldest→newest** as `(ts, value)` tuples; `metric_last` returns the latest
  `(ts, value)` or `None`. Pass `plugin_id=registry.plugin_id` (explicit, like
  `schedule_recurring` — the SDK has no ambient plugin identity; `':'` is rejected so one
  plugin can't reach another's namespace). Point-in-time snapshots stay on `telemetry()`;
  per-turn cost rollups stay on the [operator telemetry store](/guides/observability#local-telemetry-store).

The **workflows plugin** (`plugins/workflows`) is the reference consumer: its engine
injects `run_subagent` as the per-step runner. This is the pattern for plugins that tap
core, not just contribute to it.

## Events — the plugin bus (ADR 0039)

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
  ships as one consumption-SDK call: `sdk.react_on(…)` ([above](#tapping-core-deeper-graph-sdk-adr-0043)).

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

## Config, secrets & settings (ADR 0019)

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

**Conditional fields** — add `depends_on` to show a field only once a sibling is set
(e.g. an "enable X" toggle gates X's options); reactive to the in-form value:

```yaml
settings:
  - { key: ask_enabled, label: "Interactive", type: bool }
  - { key: ask_system,  label: "Ask system instruction", type: text,
      depends_on: { key: ask_enabled, equals: true } }   # also: { key, in: [...] } | bare { key } = truthy
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
The **wizard step** is not yet plugin-contributable (Settings + a docs link
suffice for now).

**Routes + surfaces are wired once at process init and don't hot-reload** — a
config reload reuses them, so changing `plugins.enabled` needs a restart
(ADR 0018). Everything is best-effort: a failing plugin/route/surface logs and
never breaks boot. The shipped [`plugins/hello`](https://github.com/protoLabsAI/protoAgent/tree/main/plugins/hello)
example demonstrates the contribution types. Plugin contributions show in
`GET /api/runtime/status`. The bundled `plugins/telegram` (the reference
`ChatAdapter`) and `plugins/github` first-party plugins are worked examples of the
contribution types; the external `discord-plugin` is a fuller surface + route + tools.

## Where plugins live & how they're enabled

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

From the console, the **Plugins** panel has a one-click **Enable / Disable** toggle per
plugin — it edits `plugins.enabled` and hot-reloads, so tools / middleware / MCP servers
apply immediately. A plugin that serves a **console view** or runs a **background surface**
(its router mounts at startup) needs a restart to finish — the toggle says so.

Plugin tools that would shadow a core or MCP tool name are skipped (logged).
Bundled skills load as `disk`-source [skills](./skills.md), re-seeded each boot.

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
