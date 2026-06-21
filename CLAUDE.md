# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Canonical instructions live in PROTO.md

**[PROTO.md](./PROTO.md) is the single source of truth** for working in this repo — and
it asks you to *edit PROTO.md, not this file*, so the agent guidance has one home and
doesn't drift. Read it before editing. It owns:

- **Run commands** — `python -m server` (never `python server.py`; retired in ADR 0023);
  `scripts/dev.sh` for the isolated dev instance; `uv sync` (Python) / `npm ci` (console).
- **The must-pass-before-PR gates** — the table mirroring `.github/workflows/checks.yml`
  (`ruff check .`, `lint-imports`, `python -m pytest tests/ -q`, `python scripts/live_smoke.py`,
  `npm run test:unit`/`test:e2e --workspace @protoagent/web`). Run them locally before the PR.
- **The gotchas that actually recur** — `F841` fails CI and isn't auto-fixed; the
  `graph/config.py` ↔ `tests/test_config_roundtrip.py` golden field map; the import-layering
  contract; `a2a_impl/` (not `a2a/`); empty `current_session_id()` inside tool bodies; the
  `*/`-in-CSS-comment minifier trap; controlled DS AppShell widths.

[README.md](./README.md) is the feature/capability map (what ships, where it lives, which
ADR governs it). Subsystem contracts are MADR ADRs in `docs/adr/NNNN-*.md` — check the
relevant ADR before changing a subsystem's behavior.

## Big picture (the cross-file model)

protoAgent is a **LangGraph agent runtime**: a Python core plus a TypeScript operator
console, extended by drop-in plugins rather than by forking.

**Request flow.** A consumer (A2A JSON-RPC over `/a2a`, the OpenAI-compatible `/v1` API,
or the React console over `/api`) hits the **FastAPI server** (`server/`, with the
operator REST surface in `operator_api/`). The server **never calls the model directly** —
it submits the message to **`graph/agent.py`** (a LangGraph `create_agent`), which owns the
tool loop, subagent `task()` delegation (`graph/subagents/`), and the `<scratch_pad>`/
`<output>` structured-output protocol. The graph talks to an **OpenAI-compatible LiteLLM
gateway** (`graph/llm.py`) — *model selection is gateway config, not code*.

**Two languages, one repo.** Python is the core (`server/ graph/ a2a_impl/ tools/
knowledge/ scheduler/ observability/ security/ infra/ runtime/ events/ operator_api/`);
TypeScript is the console (`apps/web`, the `@protoagent/web` npm workspace, served prebuilt
from `apps/web/dist`).

**Import layering is an architectural invariant, not a style choice** (enforced by
`lint-imports`): `graph/` and the infra packages must **never** import `server/` or
`operator_api/`, and `operator_api/` must never import `server/`. The `ignore_imports`
lists in `pyproject.toml` are a burndown list — remove from them, never add. (Details and
the full rule set: PROTO.md.)

**Extensibility without forking.** `plugins/` holds drop-in packages (each a repo with a
`protoagent.plugin.yaml` manifest) that add tools, `SKILL.md` skills, subagents, workflows,
FastAPI routes, background surfaces, managed MCP servers, **console rail views**, and their
own config/secrets/Settings. They're git-URL-installable and pinned in `plugins.lock`.
First-party examples live in `plugins/` (off by default). Installed/working-tree plugin
state under `config/plugins/*` and `plugins.lock` churn is expected dev-local state — don't
re-commit it.

**Instance scoping (ADR 0004).** The default/prod instance is `config/` + `~/.protoagent`
on `:7870`; the sandbox dev instance (`scripts/dev.sh`, `PROTOAGENT_INSTANCE=dev`) is
`config/dev/` + `~/.protoagent/{dev,*/dev}` on `:7871`, seeded from your default config but
with separate chat/tasks/knowledge. Use the dev instance for feature testing.
`scripts/dev-reset.sh` wipes only the sandbox.

## Forking note

This is a template repo: shared-runtime fixes (`server/`, `graph/agent.py`, extension
support, release pipeline) land here; domain-specific agent behavior belongs in a fork,
where a fork is close to rewriting `config/SOUL.md`, `graph/prompts.py`, and
`tools/lg_tools.py` and little else.
