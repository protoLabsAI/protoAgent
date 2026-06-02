# Changelog

All notable changes to protoAgent are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Add your entries under [Unreleased]** in your PR. When a release is cut,
> `prepare-release.yml` rolls them into a dated, versioned section via
> `scripts/changelog.py`. See [Releasing](docs/guides/releasing.md).

## [Unreleased]

### Added
- **Deep-research workflow with adversarial review** (ADR 0011): a bundled
  `deep-research` recipe (`run_workflow`/`/deep-research`) that orchestrates a
  six-stage DAG — `research ∥ dissent → gap_fill → antagonist ∥ verify →
  synthesize` — to fix the one-sided, self-graded ceiling of a single researcher.
  Three new subagent roles back it: an **`antagonist`** (steelmans the opposing
  case, attacks weak claims, hunts disconfirming evidence), an independent
  **`verifier`** (labels material claims supported/unsupported/uncertain), and a
  **`synthesizer`** that writes a balanced report — folding the opposition into a
  "Counterpoints & caveats" section, dropping unverified claims, and only earning
  a high `Confidence` when the opposition was answered.

### Changed
- **Researcher subagent + web-research skill upgraded** to a proper deep-research
  pipeline (lessons from rabbit-hole.io): scope a question into orthogonal
  **dimensions** (scaled quick/standard/deep), gather with **source
  diversification** (KB reuse + general + community/code) and per-dimension
  compression, run a **conservative gap-check loop** (1-3 genuine gaps, ~3
  rounds), synthesize with **numbered inline citations** (every material claim
  cited, both sides on disagreement), and **persist** one durable finding to the
  KB. The researcher gains `memory_ingest` for that persistence.

### Docs
- Reconcile drift after the recent releases: fix the deploy guide's stale
  "every merge auto-cuts a patch" note (releases are manual now), document the
  UI tiers + `--build-arg UI=full` for the image, link the orphaned "Eval your
  fork" guide, and run the OpenShell deploy example with `--ui none`.

## [0.8.0] - 2026-06-01

### Added
- **Headless setup + UI deployment tiers** (ADR 0010): `--ui {full,console,none}`
  (env `PROTOAGENT_UI`). `none` serves API + A2A + `/metrics` only — no Gradio,
  no React console — the lean headless stack. `python server.py --setup` (and
  boot-time auto-complete in the `none` tier) finishes setup from a validated
  config — no wizard. `GET /healthz` readiness probe (503 until the graph
  compiles). `gradio` is now an optional dep (`requirements-core.txt` vs
  `requirements-ui.txt`); the Docker image defaults to the lean tier
  (`--build-arg UI=full` for the all-in-one). `--headless` is a deprecated alias
  for `--ui console`.

## [0.7.0] - 2026-06-01

### Added
- **Playbooks surface** (ADR 0009) — a Knowledge ▸ Playbooks console surface to
  browse + manage the procedural-memory skill index (`skills.db`): pinned
  (SKILL.md) vs learned (agent-emitted), confidence/last-used, search, and
  delete-with-confirm. New API: `GET /api/playbooks` + `DELETE /api/playbooks/{id}`.

### Changed
- **Studio console reshaped to the control stack** (ADR 0009): tabs ordered
  Goals → Workflows → **Run** (Single/Batch is a mode on Run, not a tab);
  **Schedule** moved to **Activity** (it's a trigger, not a work-type). Skills
  now live under **Knowledge ▸ Playbooks**.
- Default model alias is now **`protolabs/reasoning`** (was `protolabs/agent`) —
  forks point at the reasoning model out of the box (override per agent in YAML).

## [0.6.0] - 2026-06-01

### Added
- **Operator primitives** (ADR 0007): a fenced multi-project filesystem toolset
  (`tools/fs_tools.py`) + project registry — opt-in, off by default. Enables a
  fork like Roxy; the agent's own repo is excluded by default.
- **Sandboxing** (ADR 0008): a deny-by-default `egress.allowed_hosts` allowlist
  enforced in `fetch_url`, and `scripts/gen_openshell_policy.py` to generate an
  NVIDIA OpenShell sandbox policy from config (project registry → Landlock
  paths, egress allowlist + gateway → network policy). New guides:
  "Build an operator fork (Roxy)" and "Sandboxing & egress".
- **Run protoAgent under OpenShell** — `deploy/openshell/` managed example:
  gateway compose + a sandbox-create script (Docker), and Helm values + an
  Agent-Sandbox CRD template (Kubernetes), policy generated from config.

## [0.5.1] - 2026-06-01

### Added
- Compaction telemetry signal (`*_compactions_total`, ADR 0006): with routing +
  tool deferral + compaction now all measured, every optimization lever the
  agent has is observable (`/api/telemetry/insights` `unproven_levers` is empty).

## [0.5.0] - 2026-06-01

### Added
- **Observability & the self-improving flywheel** (ADR 0006): measure → persist
  → surface → advise.
  - Per-LLM-call telemetry at the streaming seam: prompt-cache tokens, per-call
    latency, model, and USD cost (`pricing.py`); wired the previously-dead
    Prometheus LLM metrics (calls, latency, tokens, cache, cost).
  - `cost-v1` A2A artifact now carries Anthropic-shaped cache fields + `costUsd`
    and the agent declares the `cost-v1` extension in its card (fleet alignment).
  - Local `TelemetryStore` (per-turn rollups) + read API
    `/api/telemetry/summary` · `/recent` · `/insights`.
  - **System ▸ Telemetry** operator-console dashboard: cost, cache-hit %,
    p50/p95 latency, by-model + recent-turns tables, and an advise-only Insights
    panel (flags ≥5× median cost/latency turns, proves the cache lever in $).
  - Per-turn actual-model routing (`model`/`models`) + a
    `*_llm_tools_deferred_total` Prometheus counter proving tool deferral.

### Changed
- `costUsd` is computed in-process from a pricing table (consumers prefer it
  over recomputing from tokens).

## [0.4.0] - 2026-06-01

### Added
- MCP per-server tool allowlist (`tools.include` / `tools.exclude`) and lazy
  `enabled: false` connect, bounding the per-turn tool-schema footprint
  (ADR 0005 #1).
- Skills surface their declared `tools:` to the agent as `<relevant_tools>`
  when retrieved — a relevance hint, not a gate (ADR 0005 #2).
- Opt-in deferred tools + a `search_tools` meta-tool for progressive tool
  disclosure at high tool counts (`tools.deferred`, ADR 0005 #3).
- `CHANGELOG.md` (this file), following Keep a Changelog.

### Changed
- Releases are now cut **manually** via `workflow_dispatch` (choose
  patch/minor/major) instead of auto-bumping on every merge to `main`.
- `main` is protected by a repository ruleset: a PR and the three CI checks
  (Verify workspace config, Python tests, Web E2E smoke) are required to merge.

### Docs
- ADR 0005 — Tool Pollution & Progressive Tool Disclosure.
- Releasing runbook (`docs/guides/releasing.md`).

---

Releases cut before this changelog was introduced are recorded on the
[GitHub Releases](https://github.com/protoLabsAI/protoAgent/releases) page.

