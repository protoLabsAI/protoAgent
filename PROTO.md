# PROTO.md — agent instructions for protoAgent

The canonical instruction file for any agent (human or AI) working in this repo.
`CLAUDE.md` / `AGENTS.md` are thin pointers here — edit **this** file.

protoAgent is a LangGraph-based agent runtime with a FastAPI server, a React
console (`apps/web`), a plugin system, and an A2A surface. Python is the core;
TypeScript is the console.

---

## Run it

- **Server:** `python -m server` (never `python server.py` — single-file launch
  was retired in ADR 0023 and CI fails on it). Console is served from
  `apps/web/dist`; `/healthz` is the readiness probe.
- **Isolated dev instance (don't stomp prod data):** `scripts/dev.sh` runs a
  sandboxed instance via `PROTOAGENT_INSTANCE=dev` (ADR 0004 scoping) on `:7871` —
  its own `config/dev/` + `~/.protoagent/{dev,*/dev}` data, **seeded from your
  default config** (boots with your gateway, no re-setup) but with fresh, separate
  chat/tasks/knowledge. The default instance (`config/` + `~/.protoagent`, `:7870`)
  is untouched. `scripts/dev-reset.sh` wipes just the sandbox. Use this for feature
  testing instead of the default instance.
- **Python deps:** managed with `uv` (`pyproject.toml [project.dependencies]` is
  the source of truth; `uv.lock` is tracked). `uv sync` to install.
- **Console deps:** `npm ci` at the repo root (npm workspaces; the web app is
  `@protoagent/web`).

## Must pass before opening a PR

Run the **same commands CI runs** (`.github/workflows/checks.yml`) — locally,
before the PR, not after. CI is the merge gate; a red PR is wasted cycles.

| Gate | Command |
|------|---------|
| Lint | `ruff check .` (pinned `ruff==0.15.10`) |
| Import contracts | `lint-imports` (pinned `import-linter==2.11`) |
| Python tests | `python -m pytest tests/ -q` |
| Lean-image smoke | `python scripts/live_smoke.py` |
| Web unit | `npm run test:unit --workspace @protoagent/web` |
| Web e2e | `npm run test:e2e --workspace @protoagent/web` (Playwright/chromium) |

If a change is genuinely test-free (docs, config, pure refactor), say so
explicitly in the PR description — but that is the exception, not the default.

---

## House rules & gotchas that bite

These are the failures that actually recur — read them before you edit.

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
  events/ scheduler/ runtime/`) must **never** import `server/` or
  `operator_api/`; `operator_api/` must never import `server/`. The
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
