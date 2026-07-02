# Version coherence: why the fleet / plugins / desktop desync — and how we prevent it

> Status: analysis + remediation plan (2026-06-10). Triggered by a live debugging
> session where a rebuilt-and-restarted box kept serving "the old shit": a stale
> fleet member, 404ing plugin views, and unstyled panels. None of those were a
> single bug — they're one **class**. This doc names the class, maps every symptom
> to a root cause with `file:line` evidence, and lays out a prioritized fix plan.
> See also [[prod-readiness-tasklist]], ADR 0042 (fleet), ADR 0027 (plugins),
> ADR 0010 (UI tiers).

## The one-sentence problem

A protoAgent install runs **three things that version independently and are never
reconciled**:

1. the **core code** that each *process* is running (the hub and every fleet member are separate OS processes),
2. the **git-installed plugins** in each agent's data dir (pinned by SHA, cloned outside the app repo), and
3. the **design-system assets** each *UI tier* serves (`/_ds/plugin-kit.*`).

An install is **coherent** only when, for every running process: its core version
matches the hub's, its enabled plugins are mounted + fresh, and the DS assets it
serves match its console. **Nothing today computes, enforces, or even surfaces
that invariant** — so the three drift apart silently and the operator sees a
grab-bag of "stale" symptoms with no common explanation.

## The symptoms (this session) → the cause

| Symptom (what the operator saw) | Root cause | Axis |
|---|---|---|
| "I rebuilt + restarted the hub but the agent is still old" | The member is a **detached process** (`start_new_session=True`) that survived the hub restart — it was never re-execed. | **1 — process** |
| `project_board/board` 404; `agent_browser` no panel | Plugins **pinned to pre-fix SHAs** by the `pm-stack` bundle; a core rebuild never touches data-dir plugins, and the update button **skips pinned plugins**. | **2 — plugin** |
| Board/browser load but **styling is borked** (no DS) | `--ui none` members **don't serve `/_ds/plugin-kit.css`** — it's mounted only by the console tier — so plugin views render with no design system. | **3 — assets** |
| `doom/panel` 404 on the **fresh host**, "I updated + enabled via UI" | The view plugin's router **failed to hot-mount on the live process** (a FastAPI mount/swap limit) and the failure was **swallowed** → bare 404. A restart mounts it. | **cross-cutting — mount reliability** |
| (latent) desktop build would be worse on all of the above | `package_version()` returns **`0.0.0`** in the frozen binary, so no version-based detection can even fire. | **cross-cutting — version truth** |

All paths below are `file:line` in the repo at v0.34.0.

---

## Axis 1 — Process code staleness (the fleet)

**Members are deliberately detached and outlive the hub.** `supervisor.start`
spawns each member with `subprocess.Popen(..., start_new_session=True)`
(`graph/fleet/supervisor.py:130`); the only durable link is `fleet.json`
(`supervisor.py:35-36`). The design intent is survivability (`supervisor.py:6-7`:
"the agents outlive it"). Consequence: a **hub rebuild + restart replaces the hub
process but never signals or re-execs a running member** — it keeps the code it was
spawned with, indefinitely.

**No hub-shutdown hook stops members.** The hub's only shutdown handler
(`server/__init__.py` `@app.on_event("shutdown")` → `_scheduler_shutdown`) tears
down the scheduler, mDNS, heartbeat, Discord, and the A2A push client — it **never
touches `supervisor`/`fleet.json`/member processes**. `uvicorn.run(...,
timeout_graceful_shutdown=5)` bounds only the hub's own drain. The only callers of
`supervisor.down()` are the explicit `fleet down` CLI / `/api/fleet/down` route.

**Respawn runs current code — but only if the member was stopped first.**
`POST /api/fleet/{name}/activate` (#819) calls `supervisor.start` on slug-nav, which
rebuilds argv as `[sys.executable, "-m", "server", ...]` against the hub's *current*
checkout (`graph/workspaces/manager.py:298`) — so a *stopped* member resumes fresh.
But `start` no-ops on a live pid (`supervisor.py:121-122`) and `activate` no-ops if
already running — so a **survivor is never re-execed**. There is no "restart on
version change" anywhere.

**No hub↔local-member version reconciliation.** The #868 version handshake probes
**remote** members' A2A card and badges skew — but the **local-member** branch of
`supervisor.status()` (`supervisor.py:359-370`) emits no `version`, nothing probes a
local member, and the console badge is gated on `a.remote`
(`apps/web/src/settings/FleetManagerPanel.tsx:218`). **A local member running stale
code is completely invisible in the UI.**

> Evidence this session: the `protoPlugins` member (`:7871`) ran **11½ hours**
> across a hub rebuild to v0.34.0, serving old code + old plugins, with zero UI
> signal.

**Scoping note (#813):** a member runs with `PROTOAGENT_INSTANCE=<id>`, so its
`workspaces_root()` is its own scoped (empty) dir (`graph/workspaces/manager.py:38-53`,
`paths.scope_leaf`). A `down()`-on-exit hook running *inside a member* is therefore a
**no-op** — it can't see the hub's `fleet.json`. **Any spin-down logic must live in
the hub.**

## Axis 2 — Plugin version staleness (the data dir)

**Plugins live outside the app and a core upgrade never touches them.** `install()`
clones into `live_plugins_dir()` = `<config_dir>/plugins/<id>`
(`graph/plugins/installer.py:51-54`), gitignored (`.gitignore`: `config/plugins/`
ignored, `!plugins.lock` committed), pinned by `resolved_sha` in `plugins.lock`.
A `git pull` / new image / new `.dmg` updates **built-ins only**; installed plugins
stay at their locked SHAs forever.

**The update button skips SHA-pinned plugins — by design.**
`check_plugin_update` treats `requested_ref` matching `_SHA_RE` as **pinned** and
returns immediately with `behind: False` (`installer.py:433,439-442`). Intent:
"pinned = intentional, never auto-updates" (`docs/guides/plugin-registry.md:50-52`).
The trap: a *bundle* can pin you there without you choosing it.

**Bundles pin their sub-plugins through that same path, and there's no
bundle-level re-pin.** `_install_bundle` installs each member with the manifest's
`ref` straight through (`installer.py:256-257`) → each sub-plugin gets a normal
`plugins.lock` entry with `requested_ref = <bundle's pin>`. `pm-stack` is fetched at
HEAD on agent-create (`operator_api/fleet_routes.py:214-218`) **but its sub-plugin
refs come from whatever that manifest pins** — so latest-bundle ≠ latest-sub-plugins.
`check_updates()`/`POST /update` only ever read `lock["plugins"]`; the
`lock["bundles"]` provenance (`installer.py:259-269`) is never re-resolved. **A
bundle that pinned a sub-plugin to a SHA produces a plugin the UI can never advance.**

**The only core↔plugin compat check is one-directional.** A manifest's
`min_protoagent_version` (`graph/plugins/manifest.py:77`) is enforced at load
(`loader.py:169-195`): plugin-too-new-for-host is refused. There is **no**
plugin-too-old signal and **no** "core moved ahead, re-check freshness" step.

> Evidence this session: `pm-stack` pinned `project_board` (→ pre-#2, the `/board`
> 404) and `agent_browser` (→ pre-#7, the missing panel) to old SHAs; the per-plugin
> update button skipped both because they were SHA-pinned. Fixed by a manual
> re-install at HEAD (which also un-pins → `requested_ref=""` → future freshness
> works).

## Axis 3 — DS-asset serving gap (the fleet tier)

**The plugin-kit is served only by the console tier.**
`/_ds/plugin-kit.{css,js}` is registered inside `operator_api/web.py:mount_react_app`
(`web.py:35-46`), which is called only for the hub's console. A `--ui none` member
never mounts it.

**But a member's plugin views need it.** A plugin view page is an iframe `src` that
links `<base>/_ds/plugin-kit.css`; under the fleet proxy `base=/agents/<slug>`, so the
CSS request proxies to the **member's** `/_ds/plugin-kit.css` — which the member
doesn't serve.

> Evidence this session: member `:7871` serves `/plugins/project_board/board` (200)
> but `/_ds/plugin-kit.css` → **404** (direct and through the hub proxy). The view
> loads; it just has no design system → "borked styling."

## Cross-cutting A — Plugin-view mount reliability

A view plugin that is **enabled + installed + has a valid router** can still be
**unmounted on the live process**: hot-mounting/swapping a FastAPI router on
enable/update isn't always reliable for view plugins (the `restart_recommended`
escape hatch, #853/#887), and a plugin that wraps its `register_router` in a
`try/except` (e.g. `config/plugins/doom/__init__.py:22-27`) **swallows the failure**,
leaving a bare 404 with no surfaced reason.

> Evidence this session: `doom` is in `plugins.enabled`, installed, its
> `build_panel_router` builds `/panel` cleanly, its WASM/WAD assets are intact — yet
> `/plugins/doom/panel` 404s on the live host. A restart mounts it.

## Cross-cutting B — Version truth off-source *(fixed: #894, #1644)*

`infra.paths.package_version()` is the **one shared resolver** — the A2A card
(`server/a2a.py`), the plugin `min_protoagent_version` gate
(`graph/plugins/loader.py`), the runtime status, and the fleet version handshake
all delegate to it, so those surfaces can never disagree. It resolves
**pyproject-first, anchored to the package's own location** (never the cwd, never
an upward search — so a wheel install under someone else's project can't pick up
*their* `pyproject.toml`): the repo-root `pyproject.toml` on a source checkout /
`COPY .` image, the `_MEIPASS`-bundled copy in the frozen desktop binary, then
installed dist-info metadata (wheel installs), else `"0.0.0"`.

Two failure modes shaped that order, both closed:

- **Frozen/Docker read `0.0.0`.** The frozen binary didn't bundle
  `pyproject.toml` and Docker never pip-installs the package (only `PYTHONPATH`),
  so every off-source artifact reported `0.0.0` — no version-based detection could
  fire, and the `min_protoagent_version` gate wrongly refused every plugin that
  sets one. Fixed by bundling `pyproject.toml` at `_MEIPASS`
  (`apps/desktop/sidecar/build_sidecar.py` `BUNDLED_DATA`, #894).
- **Stale editable-install dist-info beat the pyproject.** Editable installs don't
  rewrite dist-info on a version bump (only the next `uv sync` does), so with the
  old metadata-first order a dev checkout advertised the stale version and the
  gate refused valid plugins (`requires protoAgent >= 0.78.0 but this host is
  0.72.0` — on a 0.80.0 checkout). Fixed by preferring the anchored pyproject
  (#1644).

## Desktop ("Tori") projection — every axis gets worse

- **No auto-update.** `tauri.conf.json` has no `updater` block and no `latest.json`
  is produced; update = manually swap the `.dmg`. All state (the app-config dir +
  `~/.protoagent`: plugins, `plugins.lock`, fleet workspaces) lives **outside the
  bundle** and survives every update untouched → plugin/fleet desync is structural,
  not incidental.
- **Fleet spawn isn't frozen-aware.** `manager.run_exec` returns
  `[sys.executable, "-m", "server", ...]` (`manager.py:298`); in a PyInstaller
  one-file `sys.executable` is the sidecar binary and `-m server` isn't honored — so
  **local fleet on desktop is broken**. (The MCP-plugin path was explicitly rewritten
  for the frozen case at `server/__init__.py:309-320`; the fleet path never was.)
- **Detached members outlive a `.dmg` swap.** A member spawned `start_new_session`
  keeps executing the **old** frozen binary from memory after the app is replaced —
  Axis 1, but now the two processes are *different binaries on disk*.
- **No `git` on PATH** → plugin install fails on a clean Mac (PyInstaller doesn't
  bundle `git`).
- **`0.0.0`** breaks the compat gate and every skew check (Cross-cutting B).

---

## Prevention plan (prioritized)

### P0 — make coherence true by default (cheap, high leverage)

1. **Fix version truth.** Bundle `pyproject.toml` (or a generated `_version.py`) in
   `build_sidecar.py` `BUNDLED_DATA`, and make the Dockerfile honor the already-passed
   `VERSION` build-arg. Unblocks *every* version-based detection below.
   → `paths.py`, `apps/desktop/sidecar/build_sidecar.py`, `Dockerfile`, `release.yml`.
2. **Serve `/_ds/plugin-kit.{css,js}` in every tier.** Decouple the two kit routes
   from `mount_react_app` so `--ui none` members serve them too → member plugin views
   get the design system. → `operator_api/web.py` / `server/__init__.py`.
3. **Spin members down when the hub exits — default on, opt-out.** Add
   `supervisor.shutdown_all()` (hub-only; a member's is a no-op per Axis-1 scoping) and
   call it from the hub's shutdown hook. Default **on** ("host down → fleet down is
   expected"); opt out with `PROTOAGENT_FLEET_KEEP_MEMBERS_ON_EXIT=1` for genuinely
   long-running detached agents. Sessions resume from instance-scoped checkpoints, so
   this stops *processes*, not *work*. → `graph/fleet/supervisor.py`,
   `server/__init__.py` shutdown.

### P1 — make incoherence visible (detect the rest)

4. **Probe + surface local-member version.** Generalize `refresh_remote_probes` to
   hit live local members' agent-card `version`, stamp the spawning hub's
   `package_version()` into each `fleet.json` record at spawn, drop the `a.remote`
   gate on the skew badge. → `supervisor.py`, `FleetManagerPanel.tsx`.
5. **Show "running vX.Y.Z" plainly in settings.** With version truth fixed, a `0.0.0`
   becomes a loud self-diagnosing signal.
6. **Make "pinned" advisory, not invisible.** For SHA-pinned entries, still
   `ls-remote` the default branch and report `update_available_but_pinned` (distinct
   from `behind`) so the UI can say "newer exists — reinstall at a new ref."
   → `installer.check_plugin_update`.
7. **Bundle-level update / re-pin.** `POST /api/bundles/{id}/update` re-clones the
   bundle at its latest ref and re-runs `_install_bundle(force=True)` → re-pins the
   whole tested combo. Closes the `pm-stack` trap. → `installer`, `plugin_routes`.
8. **Surface swallowed plugin mount failures.** The loader should record + surface a
   plugin whose router failed to mount (don't leave a bare 404); `PluginView` can then
   show the real reason. → `graph/plugins/loader.py`, plugin enable/update routes.

### P2 — desktop hardening (before "Tori" ships update-in-place)

9. **Make `manager.run_exec` frozen-aware** (re-invoke convention, mirroring the MCP
   path) so local fleet works on desktop and members run the current binary.
10. **Bundle `git`** into the sidecar (as Docker bundles `br`/`gh`) **or** gate plugin
    install behind a PATH-`git` probe with a clear error.
11. ~~**On app update, reconcile detached members** from `fleet.json` — offer to restart
    the ones still running the old binary.~~ **SHIPPED**: `start()` stamps the spawner's
    version on the member record; `reconcile_on_boot()` stamps each boot's version beside
    `fleet.json` and logs the update transition; `version_skew_warning()` rides the runtime
    status per poll (self-clearing) and the Fleet panel's skew badge now covers LOCAL
    members, not just remotes.

## The framing to keep

Every fix above serves one invariant: **for every running process, core version ==
hub, enabled plugins mounted + fresh, DS assets match the console.** P0 makes that
invariant *true by default*; P1 makes it *visible when it isn't*; P2 carries it into
the frozen-binary world. The bug the operator keeps hitting isn't any one of these —
it's the absence of anything that owns the invariant.
