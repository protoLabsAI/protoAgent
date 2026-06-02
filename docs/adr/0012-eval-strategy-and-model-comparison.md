# ADR 0012 — Eval strategy: model-tagged tracking & model comparison

- **Status:** Accepted (2026-06-01) — model-comparison harness + coverage cases (subagent/workflow/LLM-judge rubric) shipped
- **Date:** 2026-06-01
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** evals, observability, models, testing
- **Supersedes / Superseded by:** —

> To improve an agent you have to **measure it the same way every time** and be
> able to **swap the model and compare**. The harness already side-effect-verifies
> a turn (audit log + reply text + KB writes), but its reports weren't tagged with
> the model and there was no way to run the suite across models. We make every
> report carry the **model under test** (auto-detected from `/healthz`), add a
> `PROTOAGENT_MODEL` env override so the same agent boots against any model, and
> ship a **sweep** that boots a throwaway UI-less agent per model and prints a
> `model × category` matrix — plus a **trend** report that tracks pass rate over
> time. Output quality that substring/audit checks can't judge gets an
> **LLM-judge rubric** (follow-up slice).

---

## 1. Context & Problem Statement

The eval harness (`evals/`) is solid where it counts: each case drives the
running agent over A2A and asserts on three independent channels — the audit log
(did the expected tool actually fire?), the reply text, and KB side effects — so
a hallucinated tool result fails instead of passing. Reports land in
`evals/results/run-<ts>.json` and `evals/compare.py` diffs two of them.

But for *"track our agents as we work + swap models for testing"* two things were
missing:

1. **Reports weren't model-tagged.** A `run-*.json` recorded pass/fail/tokens but
   not *which model produced it*. After two runs you couldn't tell which was
   `reasoning` vs `agent` — so model comparison was guesswork.
2. **No model swap.** The model is fixed at boot from
   `config/langgraph-config.yaml` (`model.name`). Comparing models meant editing
   YAML, rebooting, running, and hand-tracking which report was which.
3. **No coverage for the layers we actually build.** The 15 cases test lead-agent
   tool selection + goal mode — not subagent delegation or the research/
   deep-research workflows (ADR 0002/0011), the work of recent cycles.

## 2. Decision

### 2.1 Tag every report with the model
`evals.runner` stamps `model` (and `base_url`) onto every report.
It auto-detects the model from **`GET /healthz`** (which now returns the active
`model`) so a tag is never forgotten; `--model-label` overrides. `compare.py`
and the new `report.py` key off this field.

### 2.2 `PROTOAGENT_MODEL` env override
`graph/config.py` lets `PROTOAGENT_MODEL` win over the YAML `model.name`. Swapping
the model under test becomes an env var, not a config edit — which is what makes
an automated sweep possible without mutating tracked files.

### 2.3 `evals/sweep.py` — the model-comparison run
Given `--models a,b,c`, for each model it: boots `server.py --ui none` with
`PROTOAGENT_MODEL=<model>` + a unique `PROTOAGENT_INSTANCE` (no shared data) on
its own port, waits for `/healthz`, runs the suite tagged with the model, then
tears the agent down and deletes its instance data. It prints a `model × category`
pass-rate matrix + an avg latency/token footnote and writes a combined
`sweep-<ts>.json`. UI-less + per-model instance scoping (ADR 0004/0010) is what
makes booting N disposable agents cheap and isolated. The sweep sets a bearer
token in the child env so the auth-gating cases are genuinely exercised.

### 2.4 `evals/report.py` — trend & leaderboard
Aggregates all model-tagged `run-*.json` into a **leaderboard** (latest standing
per model, best first) and a **per-model trend** (pass rate by run, with ▲/▼ vs
the previous run). This is the "track as we work" surface — a regression after a
prompt/model/code change is visible at a glance.

### 2.5 Coverage for the agent layers
- **Subagent delegation** — cases that assert the lead delegates (`task`) and the
  subagent's tools fire in the shared audit log.
- **Workflow recipes** — a `kind: "workflow"` case that drives a recipe end-to-end
  via `POST /api/workflows/{name}/run` (e.g. `deep-research`) and asserts the
  output's structure (a Counterpoints section, citations).
- **Research quality (LLM-judge rubric)** — for output quality substring checks
  can't judge ("is the report actually balanced / is the confidence earned?"), a
  grader model scores the output against a per-case rubric; the case passes above
  a threshold. This is the only way to track research quality across models.

## 3. Constraints / honest edges
- **Boot-per-model latency.** A sweep boots one agent per model (~seconds of
  startup each). That's the price of clean isolation; runtime per-request model
  binding was rejected as more plumbing + shared state between models.
- **LLM-judge is non-deterministic + costs tokens.** Rubric scores are a tracked
  signal, not a gate — pin the grader model and treat scores as trend data. Keep
  deterministic structural checks (audit/substring/KB) as the hard pass/fail.
- **Gateway aliases must exist.** `--models` are gateway aliases; a typo'd alias
  just fails to boot (reported, skipped) rather than silently scoring zero.
- Reports are local artifacts (`evals/results/` is gitignored); the trend report
  reads whatever is on the box. CI/cron persistence is a later concern.

## 4. Consequences
**Positive** — model comparison is one command (`python -m evals.sweep --models
…`); every run is attributable to a model; regressions are visible over time;
swapping the default model is a measured decision, not a vibe. Builds directly on
existing primitives (side-effect verification, `/healthz`, instance scoping, the
UI-less tier).
**Negative** — sweeps are slower than a single run; the LLM-judge adds a grader
dependency + cost; more eval surface to maintain as the agent grows.

## 5. Alternatives considered
- **Runtime per-request model override** — compare models without rebooting.
  Rejected: per-turn model binding is real plumbing and the models would share
  instance state, muddying side-effect assertions. Boot-per-model is cleaner.
- **Tag + compare only (no sweep)** — smallest change, but leaves the
  boot/run/track loop manual; the whole point was to make comparison turnkey.
- **Deterministic-only scoring** — cheap + reproducible, but can't measure the
  thing we just built in ADR 0011 (a *balanced* report). Rubric judging fills
  that, as a tracked signal not a gate.

## 6. Related
- [ADR 0006 — Observability & the self-improving flywheel](/adr/0006-observability-and-the-self-improving-flywheel) — measure → surface → advise; evals are the offline half of the loop.
- [ADR 0004 — Multi-instance data scoping](/adr/0004-multi-instance-data-scoping) & [ADR 0010 — Headless setup & UI tiers](/adr/0010-headless-setup-and-ui-tiers) — what makes a disposable per-model agent cheap + isolated.
- [ADR 0011 — Deep-research workflow](/adr/0011-deep-research-workflow) — the workflow + research-quality the coverage slice targets.
- `evals/` (`runner.py`, `sweep.py`, `report.py`, `compare.py`), `docs/guides/evals.md`.
