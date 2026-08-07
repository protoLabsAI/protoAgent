# 0096 — The self-building loop: plugin-devkit closes design → build → test → hot-swap

Status: **Proposed**

## Context

protoAgent's product thesis is an agent that **extends itself around its user**: ask for a
capability it doesn't have, and it designs, builds, tests, and hot-swaps a plugin into
itself — live, no restart. Every mechanical piece of that loop exists today, built across
four initiatives that never got a joint architectural record:

| Piece | Where | State |
| --- | --- | --- |
| Scaffold + live enable | `plugins/plugin-devkit` (#630, #902, #913) | `scaffold_plugin` writes a skeleton to the live plugins dir, enables it, hot-reloads, and verifies it actually loaded |
| Hot reload | `server/agent_init.py` `_apply_settings_changes` → `_reload_langgraph_agent` | tools, subagents, routers, surfaces, skills, workflows all reload live; module purge re-execs every plugin file |
| Coding delegates | `plugins/delegates` (ADR 0025) + `plugins/coding_agent` + `plugins/coder` (ADR 0064) | `delegate_to` over a2a/openai/acp; per-call `workdir` override is a proven programmatic seam (the board uses it) |
| Project management | projectBoard-plugin + ADR 0095 registry | full ready→worktree→PR→CI→merge loop, hard single-repo, resolved from the managed-projects registry |

There is no ADR for the loop itself — the devkit's docstring cites ADR 0027, which never
mentions it — and the seams between the pieces were never built. A 2026-08 audit of all
four subsystems found the loop open in exactly these places:

1. **The agent cannot edit what it scaffolds.** The devkit's skill, tool docstrings, and
   guide view all instruct "edit the plugin's `__init__.py`, then `reload_plugins`" — and
   no tool can perform the edit. `scaffold_plugin` writes to
   `instance_paths().plugins_dir`, which is outside the ADR 0007 fs fence, so even with
   `filesystem` enabled the dir is unreachable. Write-once, then blind.
2. **The agent cannot run the tests it scaffolds.** `with_tests=True` writes a host-free
   pytest suite plus CI — runnable only by GitHub Actions on a repo that was never
   created. Core's own test proves the missing call is trivial
   (`tests/test_plugin_scaffold.py` subprocess-runs `pytest -q` in a scaffolded dir); no
   tool exposes it. The one in-tree pytest spawner, `plugins/coder/verify.py`, still
   carries a pre-ADR-0094 hard refusal when frozen.
3. **Failure is a one-liner.** `graph/plugins/loader.py` stores `str(exc)` — no
   traceback, no location. A `NameError` arrives as `name 'foo' is not defined` while the
   agent holds neither the file contents nor a tool to change them. And
   `reload_plugins` reports every *disabled* plugin as `unknown error`, burying a real
   failure in ~14 lines of noise (its filter checks `loaded` but not `enabled`).
4. **The build lanes don't meet.** The devkit has zero awareness of `delegate_to`,
   `coder`, or the board (grep-verified in both directions); `plugin-architect` is
   text-only and its spec is consumed by nothing; the `design-plugin` workflow ends at
   the spec by design.
5. **A scaffolded plugin can't graduate.** Scaffold does no `git init`; the board is hard
   single-repo (`repo` binds once at plugin register; the store keys on
   `(db, repo, base_branch)`); the ADR 0095 registry has **no runtime write path**
   (`GET /api/projects` is deliberately read-only) — so "plugin becomes a real project
   the PM loop iterates on" requires an operator config edit and a plugin reload.
6. **The console doesn't watch the agent.** Plugin-panel invalidation fires only on
   console-initiated mutations; an agent-initiated enable shows up whenever the next
   natural `runtime-status` poll lands.
7. **Event-loop fragility.** The devkit tools call `_apply_settings_changes` (a heavy,
   serialized graph recompile) synchronously — safe today only because sync `@tool`
   functions run in LangChain's executor, with nothing guarding that accident. The
   console enable route (`operator_api/plugin_routes.py`) calls it directly *inside an
   `async def`* — the #2210 class, on the event loop.
8. **Frozen-app cracks** (not gating — the loop targets source-run first): the scaffolder
   reads `testkit.py` via `__file__`, which doesn't exist on disk in a PyInstaller
   bundle, so `with_tests=True` raises `FileNotFoundError` on desktop; pytest is neither
   bundled nor in the managed runtime's baseline.

On trust: ADR 0071's consent layer remains design-only, and the only gate on "agent
installs code into itself" is the devkit being enabled (`enabled: false` by author
default). The operator decision (2026-08-07) is to **defer guardrail work**: devkit
opt-in is the gate for now, and ADR 0081's seven-guardrail shape (off-by-default,
scoped, reversible, never-silent, lead-only) is the recorded template for the follow-up.

## Decision

Make the self-building loop a first-class product surface, owned by **plugin-devkit**,
with three build lanes sharing one spine.

### D1 — One spine, three lanes

The spine is: **design → scaffold → edit → test → enable/reload → verify-loaded → use**.
Every lane runs the same spine; they differ only in who does the *edit* step:

- **In-place** (small plugins): the lead agent edits with devkit file tools (D2) and
  iterates on `test_plugin` (D3) + `reload_plugins`.
- **Delegated** (substantial plugins): `develop_plugin` (D5) hands the edit step to a
  configured ACP coding delegate working directly in the plugin dir, then re-joins the
  spine at *test*.
- **Board-driven** (long-lived plugins): the plugin graduates to a real repo + managed
  project (D6) and iterates through projectBoard's worktree→PR→CI loop; the running
  agent consumes releases via the existing update/install path. Deliberately **not
  built in this ADR** (D7).

### D2 — Devkit file tools, fenced to the plugins root

New devkit tools `plugin_list_files` / `plugin_read_file` / `plugin_write_file`, scoped
to the union of the live plugins dir and `plugin_devkit.target_dir`. Path discipline
mirrors `tools/fs_tools.py`: every path resolves under the root; `..` and symlink
escapes are refused. The operator fs fence is **not** reused or widened — the plugins
dir is the devkit's own domain, and conflating "dirs the operator granted" with "dirs
the devkit owns" would make enabling the devkit silently widen the fs fence.

### D3 — `test_plugin`: the missing verification step

`test_plugin(plugin_id)` runs `<python> -m pytest -q` with `cwd` = the plugin dir,
bounded by a timeout and output cap, returning pass/fail plus the tail of the output.
Interpreter resolution follows the ADR 0094 pattern exactly: source-run →
`sys.executable`; frozen → `managed_python_exe()` or an actionable "install the runtime
under Settings ▸ Tools" refusal — never a system-Python fallback. When the managed
runtime lacks pytest, the failure names the fix rather than guessing. The scaffolder's
frozen `FileNotFoundError` (testkit read via `__file__`) is fixed by embedding the
testkit source as package data so `with_tests=True` works everywhere the scaffold does.

### D4 — Honest failure, or the loop can't close

The loader records the traceback (bounded) alongside `str(exc)`; `enable_plugin` /
`reload_plugins` surface it. `reload_plugins` reports only **enabled** plugins as
failures. A reload that triggers `_apply_settings_changes`' config rollback (the plugin
broke the *graph*, not just itself) says so explicitly instead of reporting a clean
reload. Iterating on a broken build is the loop's inner cycle; every one of these
messages is agent-facing UX.

### D5 — `develop_plugin`: the delegated lane

`develop_plugin(plugin_id, instructions)` resolves the configured coding delegate from
the ADR 0025 registry, dispatches it with a per-call `workdir` = the plugin dir (the
`dataclasses.replace` seam the board already uses; `manage_git` forced off for a
non-repo dir), then automatically runs `test_plugin` + `reload_plugins` and reports the
combined result. No delegate configured → the tool degrades honestly by naming what to
configure, mirroring the delegates plugin's own posture.

### D6 — Graduation: repo-from-birth, registered on request

`scaffold_plugin` gains `git_init` (init + initial commit when git is available), so a
plugin that will outlive its first session is a repo from birth — the shape `with_tests`
already assumes. A new devkit tool registers a plugin dir as an ADR 0095 managed project
(the registry's first runtime write path), **scoped to dirs under the plugins root
only** — general project registration stays an operator/console action, because a
project entry is also an fs-fence grant (ADR 0095 D3) and the agent must not widen its
own fence outside the domain it already owns.

### D7 — Board integration is a recorded follow-up, not a build

The board stays single-repo. What breaks if pointed at a scaffolded dir is now recorded
(beads workspace pinning, ready-gate path checks, gate preflight, worktree creation, PR
open) so nobody wires it naively. The graduation flow (D6) plus a second board instance
per plugin repo is config, not code; multi-board orchestration is out of scope until
real use demands it. **Security constraint carried forward:** no auto-ingestion of
outside work items into any build lane — external issues/requests reach a lane only
through an explicit operator or maintainer gate (the ADR 0071 posture; see the P2 gate
in the dogfooding roadmap).

### D8 — The console watches the agent build

Agent-initiated scaffold/enable/reload publishes a plugin-changed event; the console
subscribes and invalidates the plugin/runtime queries immediately (the
`usePluginRefresh` set), so a new rail view appears when the agent enables it, not at
the next poll. The devkit's static guide view becomes a **live status view**: plugins it
scaffolded, load state, last error (with traceback), last test result — the visible
face of the loop.

### D9 — Event-loop safety is contractual, not accidental

The devkit tools offload `_apply_settings_changes` explicitly (`asyncio.to_thread` /
executor) rather than relying on sync-`@tool` plumbing, with a comment naming the
invariant; the `plugin_routes.py` enable route gets the same #2210 treatment. A
regression test asserts no direct call from an async context.

### D10 — Deferred, by name

Consent machinery (ADR 0071 D3), self-authored-plugin history/rollback, and
lead-agent-only restriction on devkit tools are deferred by operator decision
(2026-08-07). The follow-up shape is ADR 0081's guardrail set applied to the devkit:
off-by-default flag, never-silent events (D8's event is the first piece), scoped writes
(D2/D6 deliver this), reversibility, lead-only binding. Nothing in this ADR forecloses
any of it.

## Consequences

- Ships as five slices: (1) D2+D3+D4+D9 — the in-place lane closes; (2) D5+D6 — the
  delegated lane and graduation; (3) D8 — console cohesion; (4) D6's registry write +
  D7's recorded follow-up; (5) live QA of the whole loop on a running instance plus the
  demo skill. Each slice lands with tests and doc updates (`building-plugins` SKILL.md
  currently instructs steps that require these tools to exist).
- The devkit stays `enabled: false`. Enabling it *is* the consent gate until ADR 0071 D3
  lands, and the ADR says so where an operator will read it.
- `plugins/coder/verify.py`'s legacy frozen refusal is superseded by the D3 resolution
  pattern and updated to match.
- Known sharp edge, unchanged by this ADR: disabling or re-installing a view/router
  plugin still needs a restart (FastAPI route removal), so iterative demos should
  scaffold fresh ids rather than re-install over a mounted view.

## Rejected

- **Putting the loop in core.** Extensibility is plugin domain; the devkit is already
  the authoring kit and the canonical reference plugin. Core grows only the seams
  (traceback capture, testkit-as-data, the registry write op).
- **Reusing the operator fs fence for plugin editing.** Enabling a build tool must not
  widen operator-granted filesystem access, and requiring operators to fence the
  plugins dir by hand reproduces the #2251 foot-gun.
- **Building consent machinery in this ADR.** Operator-deferred; recorded as D10 so the
  deferral is a decision, not an omission.
- **Multi-repo board support now.** The board's single-repo binding is load-bearing in
  three places; a second board instance per graduated repo covers the near-term need at
  zero code cost.
