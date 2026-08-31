# PROTO.md — agent instructions for protoAgent

The canonical instruction file for any agent (human or AI) working in this repo.
`CLAUDE.md` / `AGENTS.md` are thin pointers here — edit **this** file.

protoAgent is a LangGraph-based agent runtime with a FastAPI server, a React
console (`apps/web`), a plugin system, and an A2A surface. Python is the core;
TypeScript is the console.

---

## Run it

- **Server:** `protoagent serve` — or `python -m server`, the module form the
  frozen sidecar uses (never `python server.py`; single-file launch was retired in
  ADR 0023 and CI fails on it). The **`protoagent`** command (ADR 0075) is the
  discoverable front door: `protoagent --help` lists the management subcommands
  (`plugin` / `workspace` / `fleet` / `skills` / `config`) plus lifecycle
  (`up` / `down` / `status` / `serve` / `setup`); `protoagent up` runs the instance
  detached and `protoagent status` reports it. Both front doors route through the
  same dispatcher (`server/cli.py::dispatch`), so `python -m server <sub>` keeps
  working. Console is served from `apps/web/dist`; `/healthz` is the readiness probe.
- **Isolated dev instance (don't stomp prod data):** `scripts/dev.sh` runs a
  sandboxed instance via `PROTOAGENT_INSTANCE=dev` (ADR 0065 two-tier paths) on
  `:7871` — its whole root is `~/.protoagent/dev/` (config + every store under it),
  and it inherits the machine-wide **box** layer (`~/.protoagent/host-config.yaml`,
  gateway/model defaults) so it boots configured with fresh, separate
  chat/tasks/knowledge. The default instance is `~/.protoagent/default/` on `:7870`,
  untouched. `scripts/dev-reset.sh` wipes just the sandbox. Use this for feature
  testing instead of the default instance.
- **Spinning up a throwaway test server while the user's real instance(s) run
  (e.g. an agent booting a PR build for review): FULLY isolate it — own box root
  too, not just an instance id.** Plain `dev.sh` shares the box root (`~/.protoagent`),
  which is data-safe but trips the desktop's co-residence warning (#1552) and can
  collide on box-level resources (mDNS advertise, scheduler owner-lock). Instead:
  `PROTOAGENT_BOX_ROOT=/tmp/pa-<name> PROTOAGENT_INSTANCE=<name> python -m server
  --port <free>` — nothing under `~/.protoagent` is shared or touched. Tradeoff: a
  fresh box root does **not** inherit box config (`host-config.yaml` gateway/model
  defaults), so seed a gateway in that instance if the test needs model-backed
  features; pure-console/UI review works as-is. (Serving a worktree's own
  `apps/web/dist`: `cd <worktree> && … python -m server` — `_bundle_root()` anchors
  to the loaded `server/` package, so it serves that checkout's build.)
- **Factory-reset the current instance:** `protoagent reset` uses the same resolved
  `infra.paths` roots as the runtime, so it works for desktop, wheel, Docker and
  named instances. The next boot runs the setup wizard. **Always `--dry-run` first**
  and read the plan. A normal reset preserves sibling instances and machine-wide box
  state; if ChatGPT or Claude OAuth is present, the plan says explicitly that the
  machine remains signed in. `--purge-box` / `--all` is the handoff-machine operation:
  it removes every instance and protoAgent box-shared credential (vendor CLI login
  files remain owned by those CLIs). Other flags: `--yes`,
  `--backup`, `--keep-secrets`, `--include-dev`, and `--force` (stop a verified
  running process for this instance). `scripts/reset.sh`
  remains a thin compatibility wrapper for source checkouts.
- **See where state lives:** `protoagent config explain` (or `python -m server
  config explain`, or `GET /api/config/explain`) prints this instance's id, both roots (box + instance),
  every resolved path, and the per-field settings cascade with provenance (secrets
  redacted) — the way to answer "where is my config / where did my key go".
- **Python deps:** managed with `uv` (`pyproject.toml [project.dependencies]` is
  the source of truth; `uv.lock` is tracked). `uv sync` to install.
- **Windows checkout path:** keep the repository and its `.venv` near the drive
  root (for example `C:\src\protoAgent`). On Windows hosts without effective
  long-path support, a deep checkout can push generated dependency filenames to
  the 260-character boundary: the file installs but Python reports a misleading
  `ModuleNotFoundError`. A short checkout path avoids that host limitation.
- **Console deps:** `npm ci` at the repo root (npm workspaces; the web app is
  `@protoagent/web`). **Changing/bumping a dependency requires npm ≥ 11**
  (`npm install -g npm@11`) — see the npm-10 no-op gotcha below.
- **Console Node version:** `.nvmrc` pins **Node 20**, matching CI — `nvm use` at the repo root.
  Node 25+ pre-defines the Web Storage globals (25 enabled them by default; 20/22/23/24 don't),
  which shadowed jsdom's in the unit suite and
  failed 127 tests at `localStorage.clear()` on a clean tree; `apps/web/vitest.setup.ts` now
  repairs that so a newer Node still runs green, but `.nvmrc` is what keeps you on CI's version
  in the first place (#3213).
- **Console dev loop (frontend):** `npm run dev` (HMR) / `npm run preview` (built dist) serve
  the console on `:5173` and **proxy all backend calls (`/api`, `/a2a`, events, `/agents`,
  `/plugins`, `/_ds`) to `PROTOAGENT_API_BASE`, default `http://127.0.0.1:7871`** — the
  ISOLATED dev instance from `scripts/dev.sh`, **not** the default/prod `:7870` the desktop app
  runs. So the correct loop is *`scripts/dev.sh` (backend, :7871) + `npm run dev` (frontend)* —
  both isolated, so dev testing never touches your `~/.protoagent` data. Vite prints a loud red
  guard if you ever point `PROTOAGENT_API_BASE` at `:7870`. (Historically it defaulted to
  `:7870`, which silently crossed dev traffic into the prod/desktop instance.)

## Must pass before opening a PR

Run the **same commands CI runs** (`.github/workflows/checks.yml`) — locally,
before the PR, not after. CI is the merge gate; a red PR is wasted cycles.

**The fast gate is one command** — the same repo-owned script CI's `lint` job
invokes, so the local and CI gates can't drift:

```
python scripts/gate.py              # ruff + lint-imports + attribution + uv lock + pytest
python scripts/gate.py --lint-only  # just the lint checks (quick pre-commit smoke)
```

It runs sequentially and stops at the first failure; `uv lock --check` is
skipped with a warning when `uv` isn't installed. Cross-platform (pure Python,
no shell) — the same command works on Windows. The heavier legs below (live
smoke, web unit/e2e, Windows matrix) are *not* part of the fast gate; run them
when your change touches those surfaces. The full breakdown:

| Gate | Command |
|------|---------|
| Lint | `ruff check .` (pinned `ruff==0.15.10`) |
| Import contracts | `lint-imports` (pinned `import-linter==2.11`) |
| Attribution in sync | `python scripts/gen_attribution.py --check` (regenerate with `uv sync && uv run python scripts/gen_attribution.py` after a dep bump) |
| Python tests | `python -m pytest tests/ -q` |
| Lean-image smoke | `python scripts/live_smoke.py` |
| Web unit | `npm run test:unit --workspace @protoagent/web` |
| Web e2e | `npm run test:e2e --workspace @protoagent/web` (Playwright/chromium) |
| Changelog entry | a `changelog.d/<pr>.<kind>.md` fragment — shape and kinds in [changelog.d/README.md](./changelog.d/README.md) (bullet with a **bold lead-in** ending in `(#NNNN)`; never edit `CHANGELOG.md` directly) |
| Windows tests | A stable `Windows tests (native)` aggregate gate. Python/runtime changes run the complete `python -m pytest tests/ -q` suite minus [tests/windows_native_exclusions.txt](./tests/windows_native_exclusions.txt) across two isolated, duration-balanced `windows-latest` shards; Tauri-native changes run `cargo test --locked` on Windows. Known docs/web/marketing-only changes skip both expensive lanes, while pushes to `main` and changes to the classifier/workflow run both. Refresh the checked-in timing seed after a major suite shift with `uv run --with pytest-split==0.11.0 --with pytest --with pytest-asyncio python -m pytest tests/ -q --store-durations --durations-path tests/windows_test_durations.json`. The exclusion list is the #2412 burndown: shrink it, never grow it |

### Dependabot PRs land red on Lint — that is expected, and it is your job to fix

`gen_attribution.py` reads *installed* package metadata, so Dependabot cannot run
it: **every** dependency PR arrives failing `Attribution in sync` even when the
bump is perfectly good. Nothing is wrong with the PR; it just isn't finished. Push
the regenerated file onto the Dependabot branch:

```bash
gh pr checkout <pr>                                   # the dependabot/... branch
uv sync --frozen                                      # install THAT PR's versions
uv run python scripts/gen_attribution.py              # rewrite THIRD_PARTY_LICENSES.md
git commit -am "chore(deps): regenerate attribution" && git push
```

`--frozen` matters: a bare `uv sync` can re-resolve and quietly turn a scoped bump
into a whole-world upgrade. Check `git status` before committing — the only file
that should have changed is `THIRD_PARTY_LICENSES.md`.

Pushing to the branch has two consequences, and the second one is a trap:

1. Dependabot stops managing the PR (no more rebases), which is what you want
   at that point anyway.
2. **The PR's actor stops being `dependabot[bot]` and becomes you** — and
   `scripts/changelog_gate.sh` exempts bot PRs by exactly that check. So the
   changelog gate, green a moment ago, goes red on a dependency bump that has
   nothing to put in release notes. Apply the `skip-changelog` label; it
   re-runs its own gate, but `Verify workspace config` (which carries the twin
   check in `checks.yml`) does not re-trigger on a label event, so re-run that
   job by hand.

Two things to look at before you do any of this, because a green suite does not
cover them:

- **Dependabot edits version *constraints*, not just the lock.** A cap in
  `pyproject.toml` with a comment explaining it is documentation, not protection —
  it will happily rewrite `mcp>=1.2,<2` to `<3`. Diff `pyproject.toml` first; if a
  cap moved, the comment above it says why it was there. Caps that must never move
  belong in `dependabot.yml`'s `ignore` list.
- **A green PR only proves the workflows that run on PRs.** `desktop-build`,
  `prepare-release`, `publish`, `release`, `docker-publish` and `marketing-deploy`
  are dispatch/push-only, so an `actions/*` bump touching them ships untested —
  hold those until after a release, then dispatch the affected workflow by hand.

If a change is genuinely test-free (docs, config, pure refactor), say so
explicitly in the PR description — but that is the exception, not the default.
A change with nothing release-notes-worthy (CI plumbing, test-only) can skip the
changelog fragment with the `skip-changelog` label — applying the label re-runs
the gate on its own.

External contributors: base your branch on current `main`, and please tick
**"Allow edits by maintainers"** on the PR — without it, small fixups (a
changelog fragment, a review nit) force a maintainer to supersede the PR with a
new one instead of pushing to your branch.

## Filing issues

Issues are gated too — but only **flagged**, never blocked. The silent
`issue-gate` workflow (`.github/workflows/issue-gate.yml`) labels any issue
missing the required structure with **`needs-info`** (no comment) and removes it
once you edit the issue to conform. Use the **Bug** / **Enhancement** issue forms
— their required fields match the gate; a free-form issue needs at least a
*Problem / What's-wrong* section, plus repro + evidence (bugs) or a
proposed-direction / acceptance (enhancements). Intentional free-form → add the
`gate-exempt` label. Full checklist: **[CONTRIBUTING.md](./CONTRIBUTING.md)**.

---

## House rules & gotchas that bite

These are the failures that actually recur — read them before you edit.

- **A message's `content` already contains its `tool_use` blocks — never sum
  `content` + `tool_calls`.** LangChain's `tool_calls` is a parsed *mirror* of the same
  blocks, so any walk that counts/renders/redacts both double-processes the arguments
  (a live context audit overstated a thread by ~34k tokens this way). Walk messages
  through `graph.message_blocks` (`text_of` yields text only; `tool_calls_of` yields
  args exactly once) — and for "what's eating this thread's window", don't hand-roll:
  `python scripts/context_audit.py <session-id>`.

- **Instance paths are two-tier (box / instance) — one rule, resolve once (ADR 0065).**
  Every on-disk location comes from `infra.paths.instance_paths()` (a frozen
  `InstancePaths`): the **box** tier (`box_root` = `~/.protoagent` or `/sandbox`) holds
  machine-shared state (`host-config.yaml`, `commons/`, heartbeats); the **instance**
  tier (`instance_root`) holds this agent's config + every store. `instance_root =
  PROTOAGENT_HOME | box_root/PROTOAGENT_INSTANCE | box_root/default`. **Don't** compute
  store paths by hand or reach for the deleted `scope_leaf` / `PROTOAGENT_CONFIG_DIR`
  (both retired — desktop/Docker/fleet now set `PROTOAGENT_HOME`); add a per-store
  accessor or use `instance_paths().store("<name>")`. Identity comes from env only —
  never config-file content. `config explain` prints the resolved layout.

- **npm 10 silently no-ops workspace dependency bumps — use npm ≥ 11.** With a
  dep resolved under `apps/web/node_modules/` (e.g. `@protolabsai/ui`, nested
  because its pinned `@protolabsai/design` conflicts with the hoisted one),
  npm 10's arborist keeps the old version through **every** supported command —
  root `npm install` after a range bump, `npm install <pkg>@<v> -w
  @protoagent/web`, `npm update <pkg> -w` — even when the locked version no
  longer satisfies the manifest range. No error, nothing changes (repro'd
  three ways, 2026-07-12). npm 11 (`npm install -g npm@11`) resolves the same
  bump correctly with a plain root `npm install`. The two arborists also
  disagree about peer-stub reachability, so **npm 10's `ci` rejects an
  npm-11-generated lockfile** ("Missing: @types/react@… from lock file") —
  which is why every CI job touching the root lockfile pins `npm install -g
  npm@11` before `npm ci` (**the Dockerfile web-builder stage too** — node:20-slim
  ships npm 10; missing it broke every GHCR publish + the v0.101.0 release
  build until fixed) and the checks/desktop-build/marketing-deploy/docs
  workflows). Keep new workflows consistent. After any dep bump, regenerate
  `THIRD_PARTY_LICENSES.md` (`uv run python scripts/gen_attribution.py`) or
  the attribution gate fails.

  **This is now enforced**: root `package.json` declares `engines.npm >= 11` and
  `.npmrc` sets `engine-strict=true`, so npm 10 fails the install outright
  instead of silently building a wrong tree. Added after the no-op cost real
  debugging time: `@protolabsai/ui` sat at `0.54.1` under
  `apps/web/node_modules` while the manifest said `^0.57.0`, shadowing the
  correctly-hoisted copy. The only visible symptom was
  `currencyMathRender.test.ts` failing — on a currency guard the DS didn't ship
  until `0.55.1` — which reads exactly like a product regression, while CI (npm
  11) stayed green. `npm ls @protolabsai/ui` is the tell: it prints
  `invalid: "^0.57.0"`. If a console unit test fails locally but passes in CI,
  check the installed tree before reading the code.

- **No unused variables.** ruff selects `F` (pyflakes); `F841` (assigned-but-
  unused) **fails CI** and `ruff check --fix` does **not** auto-fix it. Don't
  leave dead locals in code or tests. (Style rules `E402/E501/E702/E731/E741`
  are intentionally ignored — lazy/late imports and 120-col comment lines are
  idiomatic here. Config: `pyproject.toml [tool.ruff]`.)

- **Bundle `config_inputs` can't target core sections.** A bundle's Configure-step
  prompts (`config_inputs:` in `protoagent.bundle.yaml`) write the operator's answer
  straight into the tracked config at the dotted key, and `required: true` is a hard
  gate (create → 400, host install → refuses to activate). So the first segment of a
  key must be a *plugin* section (`project_board.repo`, `github.default_repo`) —
  `CONFIG_INPUT_RESERVED_SECTIONS` in `graph/plugins/installer.py` rejects `model`,
  `plugins`, `projects`, `onboarding`, `delegates`, `egress`, … at install. Don't
  "fix" a failing bundle by adding its section to that set; give the plugin its own.
  Related seam: a plugin that is enabled but can't work (missing binary, no coder, CLI
  not logged in) reports it with `registry.report_setup_gap(key, message)` (clear with
  `None`) — it lands in `GET /api/runtime/status` `warnings[]`, not only in the log.
  Plugins that also run on older hosts guard it with `getattr`.

- **Config dataclass ↔ golden field map.** Adding or removing a field on the
  graph config dataclass (`graph/config.py`) requires updating the golden field
  map in **`tests/test_config_roundtrip.py`**, or the test fails with "golden
  field map is out of sync with the dataclass fields." Wire the field in all
  three places: the dataclass default, the `from_dict` parser, and the golden
  test.

- **Plugin API docs are generated — regenerate them.** Touching a public
  `PluginRegistry` method, a `graph.sdk` function, a `PluginManifest` field, the
  testkit, or the plugin CLI — *including just editing one of their docstrings or
  field comments* — makes the committed reference pages stale, and
  `tests/test_plugin_api_reference.py` fails with the file names and the fix. Run
  **`python scripts/gen_plugin_api.py`** and commit `docs/reference/plugin-*.md`.
  Two things to know: the prose comes from your docstring/comment, so a new symbol
  with none fails a *separate* assertion (write one — it's the docs); and CI builds
  your branch merged with `main`, so an upstream docstring change can make your
  pages stale even when you didn't touch those files (merge `main`, regenerate).
  The same applies to `docs/reference/plugin-view-bridge.md` when the console
  grows a `protoagent:*` bridge message (`tests/test_plugin_view_bridge_docs.py`).

- **Rebinding a core chord — or folding away a palette command — reddens a docs test.**
  `tests/test_keybinding_docs.py` re-derives both from the console source. Change a
  `defaultKeys` in `apps/web/src/keybindings/coreKeybindings.ts` and it fails with the
  exact `file:line` of every stale claim, *plus* the pages in `_MUST_STATE_THE_CHORD`
  (the guides a user learns the chord from) that would otherwise just go quiet. A claim
  is a glyph joined to the name it opens — adjacent ("⌘K clear") or across a short
  connective ("⌘⇧K / Ctrl-Shift-K **for** the command palette") — so a *historical*
  mention stays legal. Chords the desktop shell owns are read from
  `apps/desktop/src-tauri/src/lib.rs` and never judged against the in-app binding: the
  ⌥Space launcher *is* the palette, and CI must not "correct" that sentence. The third
  check reads command **names**: `press ⌘⇧K → <command>` has to name something
  `usePaletteRegistry.ts` still registers — #1769 folded **Toggle Fleet Agent** into the
  Fleet Room and the fleet guide went on telling operators to type it. #2949 swapped
  ⌘K/⌘⇧K with nothing watching, and the docs stayed inverted until #3281.

- **Import layering (enforced by `lint-imports`).** `graph/` and the infra
  packages (`a2a_impl/ observability/ security/ infra/ tools/ knowledge/
  events/ scheduler/ runtime/ ops/`) must **never** import `server/` or
  `operator_api/`; `operator_api/` must never import `server/`. (`ops/` is the
  ADR 0075 D2 shared-operation layer — one op wrapping a core, called by the CLI,
  REST, and MCP adapters; being neutral is what lets all three import it.) The
  `ignore_imports` lists in `pyproject.toml [tool.importlinter]` are a
  **burndown list** of grandfathered violations — remove entries, never add to
  them. import-linter sees function-level (lazy) imports too, so you can't hide
  one inside a function.

- **Module names.** It's `a2a_impl/` (NOT `a2a/` — that shadows the A2A SDK).
  Metrics live in `observability/` → `from observability import metrics`.
  Security helpers in `security/`, box/runtime infra in `infra/`. (Root-module
  reorg: ADR around #896.)

- **Tool / state injection.** `current_session_id()` is **empty inside tool
  bodies** (only middleware sees it). Read per-turn state via `InjectedState`
  (`ProtoAgentState`) — don't monkeypatch the resolver in tests (false
  confidence).

- **CSS comments.** Never put `*/` inside a CSS comment — it breaks the
  minifier and silently corrupts the build. Guarded by
  `apps/web/scripts/check-css-comments.mjs` (prebuild gate).

- **DS AppShell width is controlled.** Store rail widths verbatim; never
  re-clamp them (re-clamping breaks drag-to-collapse).

## Conventions

- **Match the surrounding code** — naming, comment density, and idioms. New code
  should read like the file it lives in.
- **Tests** go in `tests/` (pytest + `pytest-asyncio`); the console's in
  `apps/web/src/**/*.test.ts(x)` (vitest) and `apps/web/e2e` (Playwright).
- **Architecture decisions** are MADR ADRs in `docs/adr/NNNN-*.md`; dev notes in
  `docs/dev/`. Check the relevant ADR before changing a subsystem's contract.
- **Don't commit secrets.** A gitleaks gate runs in CI (`secret-scan.yml`).
- **Don't re-commit local churn** — `config/plugins/*` installs and
  `plugins.lock` working-tree changes are expected dev-local state.
