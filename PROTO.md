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
- **Factory-reset the default instance:** `scripts/reset.sh` wipes the **prod**
  instance back to a clean slate (next boot runs the setup wizard) — for testing the
  fresh-user flow via CLI (there is no in-app reset). **Always `--dry-run` first** to
  read the plan. It's safe on a multi-instance machine: every *other* instance
  (`~/.protoagent/<name>`, the dev sandbox, fleet members) and the machine-wide **box**
  layer (`host-config.yaml`, `commons/`) are preserved. Flags: `--yes`, `--backup`,
  `--keep-secrets` (keep gateway creds), `--include-dev`, `--force` (stop a bound
  server first). *(Reset-script rewrite for the ADR-0065 single-subtree layout is a
  follow-up; see [the env-vars gotcha](#house-rules--gotchas-that-bite).)*
- **See where state lives:** `protoagent config explain` (or `python -m server
  config explain`, or `GET /api/config/explain`) prints this instance's id, both roots (box + instance),
  every resolved path, and the per-field settings cascade with provenance (secrets
  redacted) — the way to answer "where is my config / where did my key go".
- **Python deps:** managed with `uv` (`pyproject.toml [project.dependencies]` is
  the source of truth; `uv.lock` is tracked). `uv sync` to install.
- **Console deps:** `npm ci` at the repo root (npm workspaces; the web app is
  `@protoagent/web`). **Changing/bumping a dependency requires npm ≥ 11**
  (`npm install -g npm@11`) — see the npm-10 no-op gotcha below.
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

| Gate | Command |
|------|---------|
| Lint | `ruff check .` (pinned `ruff==0.15.10`) |
| Import contracts | `lint-imports` (pinned `import-linter==2.11`) |
| Attribution in sync | `python scripts/gen_attribution.py --check` (regenerate with `uv sync && uv run python scripts/gen_attribution.py` after a dep bump) |
| Python tests | `python -m pytest tests/ -q` |
| Lean-image smoke | `python scripts/live_smoke.py` |
| Web unit | `npm run test:unit --workspace @protoagent/web` |
| Web e2e | `npm run test:e2e --workspace @protoagent/web` (Playwright/chromium) |

If a change is genuinely test-free (docs, config, pure refactor), say so
explicitly in the PR description — but that is the exception, not the default.

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
  bump correctly with a plain root `npm install`. CI is unaffected (`npm ci`
  installs the lockfile verbatim) — this bites the machine *changing* deps.
  After any dep bump, regenerate `THIRD_PARTY_LICENSES.md`
  (`uv run python scripts/gen_attribution.py`) or the attribution gate fails.

- **No unused variables.** ruff selects `F` (pyflakes); `F841` (assigned-but-
  unused) **fails CI** and `ruff check --fix` does **not** auto-fix it. Don't
  leave dead locals in code or tests. (Style rules `E402/E501/E702/E731/E741`
  are intentionally ignored — lazy/late imports and 120-col comment lines are
  idiomatic here. Config: `pyproject.toml [tool.ruff]`.)

- **Config dataclass ↔ golden field map.** Adding or removing a field on the
  graph config dataclass (`graph/config.py`) requires updating the golden field
  map in **`tests/test_config_roundtrip.py`**, or the test fails with "golden
  field map is out of sync with the dataclass fields." Wire the field in all
  three places: the dataclass default, the `from_dict` parser, and the golden
  test.

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
