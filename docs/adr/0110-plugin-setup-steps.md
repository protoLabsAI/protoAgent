# 0110 — Plugin setup steps: a host-run, operator-only fix behind a setup-gap button

- Status: Accepted
- Date: 2026-09-12
- Implemented in: #3464 (merged as `54fd3f41`) — `graph/plugins/setup_gaps.py`,
  `graph/plugins/registry.py` (`register_setup_step`), `operator_api/plugin_routes.py`
  (`POST /api/plugin-setup/{plugin_id}/{step}`), `apps/web/src/app/SetupGapBanner.tsx`
- Builds on: the setup-gap seam (#2977, #3389, #3438 — it has no ADR of its own; the
  contract lives in `graph/plugins/setup_gaps.py`),
  [ADR 0027](./0027-install-plugins-from-git-url.md) (D4 explicit dependency install, D5
  audit), [ADR 0071](./0071-plugin-permissions-trust-model.md) (trust, not sandbox; present
  only the boundary we enforce), [ADR 0066](./0066-goal-trust-operator-channel.md) (the
  `/api` operator ceiling), [ADR 0089](./0089-intra-instance-trust-boundary.md) (the
  intra-instance trust boundary), [ADR 0018](./0018-plugin-surfaces-routes-subagents.md)
  (plugin routes and their namespace)

## Context

A **setup gap** is how an installed, enabled plugin tells the operator "I can't do my job
until you do X". The plugin calls `registry.report_setup_gap(key, message, action=...)`, the
gap appears in `GET /api/runtime/status`, and the console renders it as a banner. A gap may
carry declarative **actions** from a closed vocabulary (`setup_gaps.ACTION_KINDS`). Until
#3464 the vocabulary had two kinds, `plugin_config` and `global_settings`, and both only
*navigate*: they open a settings pane. The console turns an action `kind` into a UI
affordance it already owns; no plugin string ever becomes a URL, markup or a callback.

That covers a fix that is a **setting**. It does not cover a fix that is a **command**. The
`agent_browser` plugin needs a native CLI and a Chrome to drive. A missing CLI used to leave
a banner saying "run `npm i -g agent-browser && agent-browser install` in a terminal". That
works for a developer, but a desktop user has no terminal habit and often no `npm`. The
2026-09-12 operator ruling asked for a *download, not a bundled binary*, so the fix has to
run somewhere, on the operator's say-so. An "open settings" link has nothing to open.

The host already runs one install on the operator's behalf: `plugin install-deps`
(ADR 0027 D4; ADR 0093 for frozen apps). It covers `requires_pip` only. A pinned native
binary from a GitHub release, or a ~150 MB Chrome for Testing fetched by that binary's own
`install` subcommand, is not a Python dependency.

The fix must also not open a new way to run code. Two existing rules bound the design. A
setup-gap action is data, never behavior. The only thing that can cause a plugin to install
software is an operator decision (ADR 0027 D1: install ≠ enable ≠ trust).

## Decision

Add a third action kind, **`plugin_setup`**, backed by a **setup step**. A setup step is a
zero-argument callable that a plugin registers at load time and that the **host** runs when
the operator clicks the banner button. The action names the step; the host holds the
behavior.

### D1 — `plugin_setup` is still data: it names a step, nothing more

`ACTION_KINDS = ("plugin_config", "global_settings", "plugin_setup")`. A `plugin_setup`
action is `{"kind": "plugin_setup", "step": "<id>", "label": "..."}`. It is sanitized on the
way in, like the other kinds:

- `step` must match `^[a-z0-9][a-z0-9_-]{0,63}$`: one lowercase path segment with no `/`,
  `.` or `:`, so it cannot reshape the URL it is placed into. An action with a missing or
  invalid step is **dropped**, not stored blank.
- `target` is **forced to the reporting plugin's id**, as for `plugin_config`. A gap cannot
  point its button at another plugin.
- The existing bounds still apply: at most 4 actions per gap, bounded plain-text `label`,
  and unknown keys are dropped.

Nothing in the action is ever executed, fetched, or turned into a URL.

### D2 — `registry.register_setup_step(step, fn)` is the only way to supply behavior

- `fn` takes no arguments. It returns a message string (success) or a dict
  `{"ok": bool, "message": str, "pending": bool}`.
- The host normalizes the answer:
  - The message is whitespace-collapsed and bounded (300 chars).
  - `pending` holds only when `ok`.
  - An exception becomes `ok: false` with `"<Type>: <message>"`, never a 500.
- A plugin holds at most **8** steps. Re-registering a name replaces it, which is what a
  reload does.
- A bad id, a non-callable, or a registration over the cap is **refused** with a log warning.
  It never raises into the plugin's `register()`.
- Callables are **server-side only**: kept in a process-wide map keyed by
  `(plugin_id, step)`, never serialized, never part of a gap record.
- **Long work belongs on a thread.** A step starts its download or install in the
  background, then re-reports its gap with progress and **no action**, so the button can't
  be clicked twice. It returns `pending: true`. When the work finishes it clears the gap, or
  re-reports it with the error and a Retry button.
- **Older hosts.** A plugin that must also run on them guards with
  `getattr(registry, "register_setup_step", None)`. An older host drops the unknown action
  kind, so the banner degrades to its message.
- **Tests.** `graph/plugins/testkit.FakeRegistry.register_setup_step` records steps in
  `FakeRegistry.setup_steps`, so a plugin test can call a step the way the route would.

### D3 — A core route runs exactly the registered step: `POST /api/plugin-setup/{plugin_id}/{step}`

The console button POSTs `/api/plugin-setup/<gap.plugin>/<step>`. It builds the path from the
gap's own plugin id and the server-validated step, each `encodeURIComponent`-escaped; a URL
from the payload is never used. The route:

1. **Looks up exactly `(plugin_id, step)`** in the step map and returns **404** if it isn't
   there. That covers:
   - a step the plugin never registered;
   - a step registered by a *different* plugin, because the key includes the path's plugin
     id;
   - a plugin that is disabled, uninstalled, or failed to reload.
2. Runs the callable **off the event loop** (`asyncio.to_thread`), because a step may spawn a
   process or touch the network before it hands work to a thread. If the step vanished
   between the check and the run (a concurrent disable or reload), the answer is also 404.
3. **Audit-logs** the run through the plugin installer's audit hook (ADR 0027 D5): tool
   `plugin.setup_step`, args `{id, step}`, the step's message as the summary, and
   `success = ok`.
4. Returns `{"ok", "message", "pending"}`.

The request carries no body and no arguments. Nothing from the caller reaches `fn`.

### D4 — Trust boundary: operator-only, not agent-callable, outside every plugin-exemptable prefix

- **Operator credential, always.** The route is under `/api/`, so the default-deny
  `A2AAuthMiddleware` requires a credential, and `_requires_operator` applies the ADR 0066
  ceiling: a federation-tier caller gets 403.
  - The fleet service token is operator-tier (ADR 0089 D2/D4). A hub may therefore proxy an
    *operator's* click to a member (`/agents/<slug>/api/plugin-setup/...`). The hub swaps in
    the token only for callers that cleared operator auth at its edge (ADR 0089 D3).
  - In open mode (no credential configured) the route is as reachable as every other `/api`
    route. It adds no exception of its own.
- **Deliberately outside `/api/plugins/<id>/`.** A plugin manifest may lower the gate on its
  own namespace:
  - `public_paths` exempts a prefix from auth entirely;
  - `federation_paths` lowers the operator ceiling to the federation tier (#2747).

  Both are validated against `^/(?:api/)?plugins/[^/]+/`, and a core route mounted *inside*
  that subtree inherits whatever the manifest claims there. #3464's first cut put the route
  at `/api/plugins/<id>/setup-steps/<step>`, where a manifest claiming its whole namespace
  would have made "download and run a binary" anonymous, or open to fleet peers. The
  `/api/plugin-setup/` prefix can't be matched by either key, so no manifest can reach it.
  `tests/test_plugin_setup_steps.py` pins this: with both keys claiming
  `/api/plugins/agent_browser/`, the setup route is still non-public and still operator-only.
  The same test module asserts the old path no longer answers.
- **Not agent-callable.** No agent tool, SDK helper, MCP surface or workflow step wraps the
  route or `setup_gaps.run_step`. The model can't trigger a setup step through the tool loop;
  the only trigger is an operator-credentialed POST, and in practice that is the banner
  button.
  - This is a statement about the seam, not a sandbox (ADR 0071 D1). An agent that has
    shell access and the host's privileges could install software without this route.
  - What the seam guarantees is narrower, and enforced: a plugin's registered install
    action is triggered only by an operator credential, and every run is audited.
- **Exact lookup, no indirection.** The host runs the one callable held for the one
  `(plugin, step)` pair. There is no dispatch on user input, no argv, no URL.

### D5 — Lifecycle: steps die with the plugin's live code; a generation counter fences late reports

A disabled plugin's code must not stay reachable through a stale banner button. A thread from
an old load must not bring a banner back. So:

- **Clear on disable.** The loader's disabled branch calls `setup_gaps.clear_plugin(id)`. That
  drops the plugin's gaps **and** steps.
- **Clear on a failed load or reload.** The loader's failed-load path calls `clear_plugin`
  too. Without that, a previous load's Retry button would keep running code the operator no
  longer has loaded.
- **Clear on uninstall.** `setup_gaps.retain(present_ids)` runs at the end of every
  `load_plugins` and drops gaps and steps for any id no longer on disk. Uninstalling an
  enabled plugin hot-reloads, which runs `retain`. A disabled plugin was already cleared when
  it was disabled.
- **Per-plugin generation.** `setup_gaps` keeps a monotonic clear count per plugin id. It is
  bumped by `clear_plugin`, by `retain` for each dropped id, and by the failed-load clear,
  and it is never reset.
  - A `PluginRegistry` captures the generation when it is built and passes it with every
    `report_setup_gap`.
  - A report whose generation no longer matches is dropped. Example: a download thread
    finishing after a disable can't re-raise a banner whose Retry would 404.
  - Host-side reports (e.g. "an ignored copy of this plugin is on disk") pass no generation
    and are not fenced.

### D6 — Console behavior

`SetupGapBanner` renders a `plugin_setup` action as a button: the action's `label`, or
"Run setup" when there is none.

- The button is disabled while the POST is in flight.
- The step's message is toasted.
- A `pending` answer arms a watch, and the app keeps polling runtime status until one of
  these happens: a status fetched after the click shows the gap gone (done), the gap offers
  a `plugin_setup` button again (failed, so Retry), or the watch expires.
- The console maps no other behavior to the kind.

### First users (agent_browser)

- **`download-cli`** (`plugins/agent_browser/setup_steps.py` → `cli_fetch.py`) fetches
  agent-browser **v0.27.1**:
  - one asset per platform, whose SHA-256 is pinned in code (upstream publishes no checksum
    file) and verified **before** anything is written;
  - installed by temp file + `os.replace` into the box-tier cache under
    `instance_paths().cache_dir`;
  - every URL and redirect hop goes through the egress policy (ADR 0008), and a redirect must
    stay on HTTPS `*.githubusercontent.com`.

  The click is the operator's explicit choice, so it retries even when `cli_autofetch` is
  off.
- **`install-chrome`** (`chrome_install.py`) runs the resolved CLI's own
  `agent-browser install`:
  - as the root of its own process group, with pipes drained on threads and a 20-minute
    bound;
  - on timeout the whole tree is killed (`infra.proc.kill_tree`, ADR 0098).

  It runs **only** from this button, never from a tool call. On Linux ARM64, where Chrome for
  Testing has no build, the gap points at the distro's Chromium instead of offering a button
  that can only fail.

The CLI has a second path besides this seam. The plugin *also* downloads it inline on first
use of a browser tool or the panel when its `cli_autofetch` setting is on (the default; the
tool waits at most 2 minutes, then reports "still downloading"). That is plugin
configuration, not the setup-step seam, and it is scoped to the pinned, hash-verified CLI.
Chrome, the large download into the operator's home directory, is never fetched without the
click.

## Alternatives considered

- **Extend pip-only "Install deps" (ADR 0027 D4 / ADR 0093).** Rejected. It installs
  `requires_pip` wheels, and neither a native CLI release asset nor a browser is one. Teaching
  the core install path each plugin's bespoke installer would move per-plugin behavior into
  core. The setup-step seam keeps the behavior with the plugin and puts only the dispatch,
  auth and audit in core.
- **A plugin-owned route under `/api/plugins/<id>/`.** Rejected. That covers both a router
  the plugin mounts itself (ADR 0018) and a core route inside that subtree (#3464's first
  cut). The subtree is exactly the one a manifest's `public_paths` / `federation_paths` can
  un-gate, so a plugin's own manifest could make its "install software" endpoint anonymous
  or fleet-reachable. Every plugin would also re-implement 404-after-disable, off-loop
  execution and audit. The console would need a per-plugin URL, which breaks "a plugin
  string never becomes a URL".
- **Auto-run the fix on the tool call that hits the gap.** Rejected for the seam, and for
  anything large or outside the plugin's own cache. A tool call is model-initiated, and
  "download 150 MB of Chrome into your home directory" is an operator decision. The steps
  therefore have no agent path at all (D4). The plugin's own first-use CLI download (see
  First users) is a narrower, setting-controlled exception, and it doesn't go through the
  seam.
- **Put a command or URL in the action and have the console or host run it.** Rejected. It
  breaks the closed-vocabulary rule that makes setup-gap actions safe to render. A string
  from the plugin would become behavior.
- **Bundle the binary in the app.** Rejected by the 2026-09-12 operator ruling (a download,
  not a bundled binary): a second binary to ship and notarize, paid for by users who never
  drive a browser. This is the same trade as ADR 0085's on-demand Node.

## Consequences

- A fix that is a command becomes one click, and the banner reports progress and the outcome
  without a reload. `agent_browser` no longer needs a terminal, `npm`, or a bundled binary.
- The operator is the only trigger, the route can't be exempted by any manifest, and every
  run is in the audit log. Those are the boundary we enforce, and ADR 0071 D2 asks us to
  present only that.

**What a third-party plugin can do:**
- Register up to 8 named steps.
- Have one run in the host process, with the host's privileges (ADR 0071 D1), when an
  operator clicks its banner button (or makes an equivalent operator-credentialed POST).
- Report progress through its gap.
- Degrade cleanly on older hosts.
- Unit-test its steps with `FakeRegistry`.

**What a third-party plugin cannot do:**
- Run a step without the operator credential, or lower that gate from its manifest.
- Expose a step to fleet peers or to its own agent's tools.
- Name another plugin's step, or aim its button at another plugin.
- Put a URL, command or markup into the action.
- Pass arguments to its step from the request.
- Keep a step reachable after it is disabled, uninstalled, or fails to reload.
- Resurrect a banner from a load that has since been cleared.
- Turn a step's exception into a 500.

**Negative consequences and known limits:**
- **No host timeout.** The step call is synchronous in a worker thread, so a step that
  blocks instead of backgrounding its work holds the request. The contract says to start
  long work on a thread; the host doesn't enforce it.
- **Partial audit.** The audit records the step's *first* answer, not the background
  outcome. The final outcome shows only in the gap.
- **A successful hot reload doesn't retire the generation.** Old and new loads of a
  still-enabled plugin share it. A step name that a new version stopped registering stays
  bound to the old callable until the next disable, uninstall or failed reload.
  `register_setup_step` itself isn't generation-fenced; registration is expected to happen
  in `register()` at load time.
- **No self-healing.** Operator-only means an agent or a fleet peer can't fix the gap on its
  own. That is the intent, but an unattended member keeps its banner until someone clicks.

## Refs

#3464 (this seam, and the agent_browser CLI download and Chrome install buttons) · #3389
(structured setup gaps and declarative actions) · #3438 (console reads `setup_gaps[]`) ·
#2977 (the setup-gap seam) · #2747 (`federation_paths`) · ADR 0027 · ADR 0066 · ADR 0071 ·
ADR 0089 · ADR 0098
