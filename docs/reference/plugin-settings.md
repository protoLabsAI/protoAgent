# Plugin settings and setup contracts

Use [Build plugins](/guides/plugins#config-secrets-settings) to add a minimal settings form.
This reference owns field types, Configure tabs, required settings, setup gaps,
and reload behavior. The [manifest reference](/reference/plugin-manifest) owns
the top-level manifest fields.

## Declare settings {#config-secrets-settings}

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

**One save model per tab — `apply: live` (#4489).** By default a schema tab is
*save-then-apply*: tab switching retains one shared dirty state and the tab's **Save &
apply** button submits every tab's pending changes together. Add `apply: live` to a tab
descriptor to switch it to the *live* model instead — its fields save the moment they
change, with a small inline "Saved" (or the error, saying nothing changed), and the tab
shows no Save or Discard. A plugin-owned (path-backed) tab is **always live**; its page
owns its own writes. `live` is the only value honored — any other (a typo, `true`) is
ignored with a warning and the tab keeps save-then-apply — and the key is additive, so a
plugin that omits it is unchanged. Never put a live control next to a Save button: give
each tab one model.

```yaml
settings_tabs:
  - { id: overview, label: Overview, apply: live }   # fields save on change
  - { id: runtime,  label: Runtime }                 # save-then-apply (the default)
  - { id: projects, label: Projects, path: /plugins/boardy/config/projects }  # path = live
```

**Restart-only changes are collected, not badged (#4489).** A setting marked
`restart: true` no longer carries a per-field "restart" badge. After it saves, its label
joins a single banner at the top of the pane — *"N changes take effect after a
restart"* — one list per plugin pane, fed by the `restart_required` keys of every save. A
plugin-owned page can add its own restart-only changes to the same banner by posting
`{type: "protoagent:settings:restart-pending", keys: [<labels>]}` to its host
(`window.parent`); the host trusts it only from the page's own origin, the same check
every page→host message passes. The banner holds a slot for a **Restart now** action
where the host offers one (protoAgent#4490); until then it explains how to restart. Prefer
making a setting live-reloadable over marking it `restart: true`. See the full
[settings standard](/guides/plugin-settings).

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
Declare required values with `required: true`; the console surfaces the
**incomplete** state until the operator supplies them.

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

## Reload behavior

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

