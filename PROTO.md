# PROTO.md — agent instructions for protoAgent

The canonical instruction file for any agent (human or AI) working in this repo.
`CLAUDE.md` / `AGENTS.md` are thin pointers here — edit **this** file.

protoAgent is a LangGraph-based agent runtime with a FastAPI server, a React
console (`apps/web`), a terminal fleet deck (`deck/`, Textual), a plugin system,
and an A2A surface. Python is the core; TypeScript is the console.

---

## Run it

- **Server:** `protoagent serve` — or `python -m server`, the module form the
  frozen sidecar uses (never `python server.py`; single-file launch was retired in
  ADR 0023 and CI fails on it). The **`protoagent`** command (ADR 0075) is the
  discoverable front door: `protoagent --help` lists the management subcommands
  (`plugin` / `workspace` / `fleet` / `skills` / `config`) plus lifecycle
  (`up` / `down` / `status` / `serve` / `setup`); `protoagent up` runs the instance
  detached and `protoagent status` reports it. For an installed package, use `protoagent`; a source checkout uses
  `uv run python -m server` because uv does not install the project CLI. Both front doors route through the
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
- **Console Node version:** `.nvmrc` pins Node 20, matching CI. Run `nvm use` at
  the repository root. The unit setup repairs Web Storage globals on newer Node
  versions, but use the pinned version when comparing local results with CI.
- **Console dev loop (frontend):** `npm run dev --workspace @protoagent/web` (HMR) /
  `npm run preview --workspace @protoagent/web` (built dist) serve
  the console on `:5173` and **proxy all backend calls (`/api`, `/a2a`, events, `/agents`,
  `/plugins`, `/_ds`) to `PROTOAGENT_API_BASE`, default `http://127.0.0.1:7871`** — the
  ISOLATED dev instance from `scripts/dev.sh`, **not** the default/prod `:7870` the desktop app
  runs. Use *`scripts/dev.sh` (backend, :7871) +
  `npm run dev --workspace @protoagent/web` (frontend)* —
  instance data is separate, while box-level config and credentials remain shared. Vite prints a loud red
  guard if you ever point `PROTOAGENT_API_BASE` at `:7870`. (Historically it defaulted to
  `:7870`, which silently crossed dev traffic into the prod/desktop instance.)

## Must pass before opening a PR

Run the **same commands CI runs** (`.github/workflows/checks.yml`) locally before
opening a PR.

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
| Python tests | `python -m pytest tests/ -q` — CI adds `-n auto` (`pytest-xdist==3.8.0`), so tests must not depend on file order or leak process-global state (reset it in `tests/conftest.py`) |
| Lean-image smoke | `python scripts/live_smoke.py` |
| Web unit | `npm run test:unit --workspace @protoagent/web` |
| Web e2e | `npm run test:e2e --workspace @protoagent/web` (Playwright/chromium) |
| Changelog entry | a `changelog.d/<pr>.<kind>.md` fragment — shape and kinds in [changelog.d/README.md](./changelog.d/README.md) (bullet with a **bold lead-in** ending in `(#NNNN)`; never edit `CHANGELOG.md` directly) |
| Windows tests | A stable `Windows tests (native)` aggregate gate. Python/runtime changes run `python -m pytest tests/ -q -m platform_sensitive` (the OS-touching ~27% of the suite, minus [tests/windows_native_exclusions.txt](./tests/windows_native_exclusions.txt)) across two isolated, duration-balanced `windows-latest` shards; a nightly `schedule` run covers the whole suite. **A test that spawns processes, signals, locks, chmods/symlinks, `os.replace`/`os.rename`s, reads `sys.platform`/`os.name`, or imports code that branches on the platform must carry `@pytest.mark.platform_sensitive`** (or the module's `pytestmark`) — `tests/test_platform_sensitive_marks.py` fails otherwise; Tauri-native changes run `cargo test --locked` on Windows. Known docs/web/marketing-only changes skip both expensive lanes, while pushes to `main` and changes to the classifier/workflow run both. Refresh the checked-in timing seed after a major suite shift — **on Windows only** (a macOS seed skews the split, #3892) — by running the `Windows test durations` workflow (`gh workflow run windows-test-durations.yml`; it also runs monthly and opens a PR with the new seed), or by hand on a Windows box with `uv run --with pytest-split==0.11.0 --with pytest --with pytest-asyncio python -m pytest tests/ -q --store-durations --clean-durations --durations-path tests/windows_test_durations.json`. The exclusion list is the #2412 burndown: shrink it, never grow it |

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

## Landing a PR

Enable auto-merge when opening a PR:

```bash
gh pr create ... && gh pr merge --auto --squash --delete-branch
```

`main` uses a repository ruleset. Inspect it with
`gh api repos/:owner/:repo/rulesets`; the legacy branch-protection API can return
404 for a ruleset-protected branch.

**Confirm review covers the current head before landing.** `Review at head` is a
required check comparing the PR's head with the panel's verdict. It does not
judge verdict quality: a `WARN` passes this check. The panel's own `QA panel`
status is not a required ruleset check, so inspect the review too.

```bash
gh pr view <N> --json headRefOid -q .headRefOid
gh api repos/:owner/:repo/pulls/<N>/reviews \
  -q '.[]|select(.user.login=="protoreview[bot]")|.body'
```

Panel reviews record `head=<12-character SHA>`. If the verdict matches but
`Review at head` is stale, dispatch `gh workflow run "Review at head"`. The
scheduled sweep can be delayed; `pull_request_review` events only help branches
that contain the workflow. The `skip-review-gate` label waives the check with a
recorded status description when a verdict cannot be obtained. Implementation:
`scripts/review_at_head.py`; tests: `tests/test_review_at_head.py`.

A red **“QA panel: N finding(s) persist”** counts unresolved review threads,
including other reviewers' threads. Read the threads even if the panel returned PASS.

**For sliced work, only the final slice says `Fixes #N`.** Earlier slices say
`Refs #N` so the issue remains open until all acceptance criteria are delivered.

### Epic branches: review slice by slice, land the epic by attestation

A feature too big for one PR ships on a long-lived `epic/<name>` branch (ADR 0114 and
ADR 0118 ran this way). **The slice PRs are the review of record**, so the reviewer never
has to read the whole epic as one diff at the end.

- **Each slice PR targets `epic/<name>`, never `main`, and gets the full panel.** CI
  runs on `epic/**` bases. A slice merges into the epic only on a **complete** PASS at
  its head. A `neutral` `QA panel` ("Incomplete pass", `hold:incomplete-coverage`)
  means a finder lane did not run and that part of the diff is unreviewed. Re-run the
  panel (`@vera review` on the PR) before merging. The board enforces this for projects
  with `require_complete_review: true` (projectBoard-plugin#520).
  If the re-review is **also** incomplete, the panel cannot finish this diff. Seen on
  #4101: a finder lane ran its whole output budget as reasoning and returned no answer.
  Run an adversarial review subagent over the slice instead, post its result on the PR,
  and merge by hand. A hand merge skips the gate, so never merge without that substitute
  review.
- **Do not put `merge-hold` on slices bound for the epic.** The operator's hands-on test
  happens once, on the epic, before it goes to `main`. A per-slice hold only freezes
  the dependency chain behind it.
- **Sync `main` into the epic regularly.** A clean merge introduces nothing new to
  review. If the sync conflicts, resolve it in its own small PR into the epic, so the
  resolution is reviewed like any other slice.
- **The epic → `main` PR is reviewed by attestation, not re-review.** The reviewer
  attributes every commit in `main..epic/<name>` to a slice PR with a complete PASS, or
  to a clean sync merge. It reviews only what is left over (direct pushes, conflict
  fixes, slices with incomplete reviews) and posts an attestation table as its
  verdict (pr-reviewer-plugin#271, `pr_reviewer.epic_attestation`, on by default). Merge
  the epic with a **merge commit**, not a squash, so each slice stays
  visible in `git log` and `blame`.
- **Only the epic → `main` PR says `Fixes #<epic issue>`.** A slice says
  `Refs #<epic issue>` plus `Fixes` for its own slice issue; otherwise the first slice
  merge closes the epic.

## Filing issues

Issues are gated too — but only **flagged**, never blocked. The silent
`issue-gate` workflow (`.github/workflows/issue-gate.yml`) labels any issue
missing the required structure with **`needs-info`** (no comment) and removes it
once you edit the issue to conform. Use the **Bug** / **Enhancement** issue forms
— their required fields match the gate; a free-form issue needs at least a
*Problem / What's-wrong* section, plus repro + evidence (bugs) or a
proposed-direction / acceptance (enhancements). Intentional free-form → add the
`gate-exempt` label. Full checklist: **[CONTRIBUTING.md](./CONTRIBUTING.md)**.

### Getting an issue picked up: the `team-ready` label

Filing an issue does not queue it. **`team-ready` is the only intake gate the board
pipeline accepts** — without it an issue is invisible to autonomous dispatch, no
matter how well written or how high its priority.

```
gh issue edit <N> --add-label team-ready
```

Nothing in this repo dispatches. `team-ready-claims.yml` (logic in
`scripts/team_ready_claims.py`) only *reconciles* the label: an open PR saying
`Closes #N` swaps `team-ready` → **`claimed-by-pr`** so a second agent doesn't
burn a run on work already in flight, and the swap reverses if that PR is
abandoned. Seeing the bot remove your label is the pipeline working, not a fault.

**Two things decide whether a labelled issue actually moves:**

- **The body has to stand alone.** A dispatched agent gets the issue, not the
  conversation that produced it — so decisions belong in the body, not in a
  comment thread below it, and a stale body is worse than a thin one. If work has
  landed since filing, rewrite the body (and the *title*) to what is genuinely
  left before labelling, or the agent rebuilds what already shipped.
- **A decision is not a task.** If part of the scope is an operator or security
  call, say so in the body and tell the agent to stop and comment rather than
  decide. Otherwise it will pick one.

Corollary for anything you want done autonomously: an empty `team-ready` queue is
the normal state here, and it reads exactly like a broken pipeline. Check the
label before concluding the loop is stuck.

---

## House rules & gotchas that bite

- **Test console interactions in the desktop shell.** A green Chromium suite
  does not establish WKWebView or WebView2 behavior. Every desktop
  `WebviewWindowBuilder` must keep `.disable_drag_drop_handler()` so HTML
  drag-and-drop reaches the page; the console does not consume native
  `tauri://drag-*` events. When a report contradicts the suite, establish which
  surface was tested. Probe drag behavior with raw `page.mouse.down/move(steps)/up`
  as well as `locator.dragTo()`. Use `setDragImage(<row>, …)` for small handles and
  populate `dataTransfer` so Firefox can start the drag.

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

- **Use npm ≥ 11 for every workspace install and dependency change.** npm 10
  can retain an outdated nested dependency after a range bump and rejects some
  npm-11-generated lockfiles. Root `engines.npm` and `.npmrc` enforce the version.
  Pin `npm install -g npm@11` before `npm ci` in new CI jobs and Docker builders.
  If a local web test fails while CI passes, inspect the installed tree with
  `npm ls @protolabsai/ui` before changing code. Regenerate
  `THIRD_PARTY_LICENSES.md` after dependency changes.

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
  `CONFIG_INPUT_RESERVED_SECTIONS` in `graph/plugins/bundles.py` rejects `model`,
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

- **Update docs when changing shortcuts or palette commands.**
  `tests/test_keybinding_docs.py` derives shortcuts from
  `apps/web/src/keybindings/coreKeybindings.ts`, desktop global shortcuts from
  `apps/desktop/src-tauri/src/hotkeys.rs`, and command names from
  `usePaletteRegistry.ts`. It checks both stale claims and required mentions in
  `_MUST_STATE_THE_CHORD`. Preserve historical mentions when useful, but teach
  only current commands. The desktop ⌥Space launcher is also a palette; do not
  compare it with the in-app binding.

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

- **The fleet deck (`deck/`) is an HTTP client of the hub, never an importer of
  the core.** It must not import `graph/`, `server/` or `operator_api/` (a
  `lint-imports` contract); `graph/fleet/cli.py` imports the deck and hands it what
  it needs from the core as callables (peer discovery, the hub launcher). Textual
  is imported by name only when the deck opens — `--help` and every
  non-interactive verb stay Textual-free (`tests/test_fleet_cli.py` asserts it per
  verb), which is why the hub model shared with the CLI lives in the Textual-free
  `deck/discovery.py`. Any hub- or member-authored string put into a widget must
  be a `rich.text.Text`: Textual parses a plain `str` as markup, and a stray `[/]`
  raises on the UI thread (`tests/test_deck_markup.py`). The frozen desktop binary ships
  the deck, and every desktop-build leg runs it there (`scripts/fleet_deck_smoke.py --bin`,
  #3498) through the hidden `protoagent fleet --self-check` — the deck under Textual's
  headless driver over an in-memory roster, painting roster, filter, detail and hub tree.

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

## Documentation changes

- Lead task guides with prerequisites, the action, and the expected result. Put
  implementation details and migration history after the procedure or link to
  reference/explanation pages. Preserve useful URLs and anchors when moving content.
- Check commands, UI labels, config paths, and defaults against source. Source
  checkouts need a frontend build and use `uv run python -m server`; packaged
  installs use `protoagent`.
- After sidebar edits, run `python scripts/gen_docs_nav.py` so in-app help matches
  the site. Keep primary task pages in the corpus sections (`tutorials`, `guides`,
  `reference`, `explanation`, `adr`).
- Validate with `npm run docs:build`, the docs-related tests, and generated-doc
  checks. Change generated reference prose at its source, then regenerate it.

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
