# ADR 0010 — Headless setup & UI deployment tiers (lighter stack)

- **Status:** Accepted (2026-06-01) — design/decisions; implementation to follow ("we'll go")
- **Date:** 2026-06-01
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** deployment, setup, ui, dependencies, headless, docker, operator
- **Supersedes / Superseded by:** generalizes the existing `--headless` flag

> Accepted. Two coupled needs: (1) **complete setup without the wizard UI** —
> drop a config, supply secrets, the graph compiles (Roxy hit the
> `.setup-complete` gate with no UI to satisfy it); and (2) **a lighter stack
> that doesn't deploy the UI at all** — API + A2A only, without the React console
> or the heavy Gradio dependency. Decision: a single **`--ui {full,console,none}`**
> deployment tier (env `PROTOAGENT_UI`), a **headless setup** path (a `--setup`
> one-shot + boot-time auto-complete from a valid config), and **`gradio` becomes
> an optional dependency** so `none`/`console` stacks are lean.

---

## 1. Context & Problem Statement

Current behavior:

- **Setup is wizard-gated.** `_init_langgraph_agent` (`server.py:138`) refuses to
  compile the graph until `is_setup_complete()` sees the `.setup-complete`
  marker, and **only the wizard** (`/api/config/setup` → `mark_setup_complete()`)
  writes it. A headless deploy that drops a valid `langgraph-config.yaml` still
  boots with `_graph = None` ("Open the UI to finish setup") — there's no UI to
  open. (Roxy hit exactly this.)
- **`--headless` is half a tier.** It skips **Gradio** (the heavy,
  PyInstaller-hostile dep) but **still serves the React console** at `/app` +
  `/static` (`server.py:2608-2633`). There's no "no UI at all" mode.
- **Gradio is a hard dependency** (`requirements.txt: gradio>=5.0`) — installed
  even for an API/A2A-only server that never imports it.

So a headless agent (Roxy, any server summoned via A2A) can't (a) finish setup
without a UI, or (b) run a lean stack without the console + gradio.

## 2. Decision

### 2.1 One deployment tier: `--ui {full,console,none}` (env `PROTOAGENT_UI`)

| Tier | Gradio (`/`) | React console (`/app`, `/static`) | API + A2A + `/metrics` | For |
|---|---|---|---|---|
| `full` | ✅ | ✅ | ✅ | local dev / `python server.py` (default) |
| `console` | ✕ | ✅ | ✅ | desktop sidecar (React is the UI) — today's `--headless` |
| `none` | ✕ | ✕ | ✅ | **headless servers / Roxy — the lighter stack** |

- `none` skips the Gradio import/mount **and** the React-console + `/static`
  mounts — pure API + A2A + `/metrics`.
- `--headless` / `PROTOAGENT_HEADLESS` is kept as a **deprecated alias for
  `--ui console`** (back-compatible; logs a deprecation note).
- Default stays `full` for `python server.py` so local dev is unchanged.

### 2.2 Headless setup (no wizard)

Two ways to satisfy the `.setup-complete` gate without a UI:

1. **`python server.py --setup`** — one-shot: `ensure_live_config()`, validate
   the live config, then `mark_setup_complete()` and exit. Idempotent; the
   explicit operator/CI path.
2. **Boot-time auto-complete** — when setup isn't complete but the config
   **validates** and the tier is `none` (or `PROTOAGENT_HEADLESS_SETUP=1`),
   mark complete and compile. So a container just mounts a config + supplies the
   key + runs.

**Validation (shared helper `validate_for_headless(config) -> (ok, reason)`):**
config parses, `model.api_base` is set, and the model `api_key` resolves
(`config/secrets.yaml` **or** `OPENAI_API_KEY` env). **Fail fast** with the
concrete reason if not — never silently mark a broken config complete, never
boot a dead graph in a headless tier.

> Why gate auto-complete on `none`/an env, not always: in `full`/`console` the
> wizard is reachable and is the intended first-run funnel; auto-completing
> there would skip credential collection. Headless tiers have no wizard, so
> auto-complete (or `--setup`) is the only path.

### 2.3 `gradio` becomes optional (the lighter stack)

- Split deps: **`requirements.txt` = core** (API / A2A / graph / stores — no
  gradio); **`requirements-ui.txt` = gradio** (the `full` tier only).
- `full` imports gradio lazily (already does); if it's missing, fail with a
  clear message: *"gradio not installed — `pip install -r requirements-ui.txt`
  or run `--ui console|none`."*
- **Docker default = the lean stack**: core deps, `PROTOAGENT_UI=none` — the
  image is almost always a headless server. A `--build-arg UI=full` (or a
  separate stage) adds gradio + the console for an all-in-one image.

## 3. Mechanism summary (for the build)

- `_main`: replace the bool `headless` with a `ui` tier resolved from `--ui` /
  `PROTOAGENT_UI` / (deprecated) `--headless`. Gate the Gradio block on
  `ui == "full"`, and the React-console + `/static` mounts on `ui != "none"`.
- `--setup` subcommand/flag → validate + `mark_setup_complete()` + exit.
- `_init_langgraph_agent`: if `not is_setup_complete()` → if headless-setup
  conditions hold and the config validates, `mark_setup_complete()` + continue;
  else keep the current `_graph=None` + log (full/console show the wizar­d).
- `validate_for_headless` in `graph/config_io.py` (next to the marker helpers).
- `requirements.txt` slimmed; `requirements-ui.txt` added; Dockerfile build-arg.

## 4. Security / safety

- **Fail-fast, never silent.** A headless tier with an invalid/cred-less config
  exits with the concrete reason — it does not mark setup complete or serve a
  dead graph.
- **Secrets stay out of the config** (ADR 0008 / secrets-overlay): the key
  resolves from `secrets.yaml` or env; `--setup` never writes secrets to the
  tracked YAML.
- **Smaller attack surface** in `none`: no console, no Gradio, fewer deps.

## 5. Consequences

**Positive** — headless agents (Roxy, A2A-summoned servers) go config → running
with no UI; the default image is lean (no gradio, no console); one tier knob
replaces the half-measure `--headless`.

**Negative / costs** — `full` now needs `requirements-ui.txt` too (documented;
local-dev quickstart updated). The dependency split + Docker build-arg touch the
deploy docs. `--headless` becomes a deprecated alias (kept working).

## 6. Alternatives considered

- **Always auto-complete setup when a config exists** — rejected: would skip the
  wizard's credential funnel in `full`/`console`. Gated to headless tiers.
- **Keep gradio required, just don't import it** — leaves the heavy dep in every
  image; rejected for the lighter-stack goal.
- **A separate `protoagent-core` package** — cleaner long-term, heavier now;
  the requirements split + build-arg gets 90% of the win without repackaging.

## 7. Open questions

- Should the Docker default be `none` (lean, surprises full-image users) or
  `full` (heavy, safe)? *Leaning `none`* — images are servers; `--build-arg UI=full`
  for the all-in-one.
- `--setup` as a flag on `server.py` vs a `scripts/setup.py` CLI? *Leaning a
  flag* (one entrypoint).
- Do we also want a `GET /healthz`/readiness signal that reflects "graph
  compiled" for the `none` tier (no UI to eyeball)? Likely yes — small follow-up.

## 8. Related

- [ADR 0007 — Operator Primitives](/adr/0007-directory-aware-operator-agent) — Roxy, the prime headless tenant.
- [ADR 0008 — Sandboxing](/adr/0008-sandboxing-and-openshell) — secrets-overlay + the OpenShell deploy this complements.
- Code: `server.py` (`_main`, `_init_langgraph_agent`, the mount blocks),
  `graph/config_io.py` (`is_setup_complete` / `mark_setup_complete`),
  `requirements.txt`, `Dockerfile`.
