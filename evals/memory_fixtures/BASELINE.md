# PV4 memory-retrieval baseline

> **STATUS: BLOCKED — not generated.** The extraction model resolved to the
> **main model** (`routing.aux_model` is blank), so the r1 guard fired. No
> fixtures were extracted and no retrieval baseline was run. This file records
> the block and its remediation; it intentionally contains **no pass counts**,
> because no run happened. See *Why this is blocked* below.

| Field | Value |
| --- | --- |
| Date | 2026-11-08 |
| Git sha | `500aaf6aa2898cfdd12e795f79205e4a5e1c5533` |
| Instance config | `~/.protoagent/default/config/langgraph-config.yaml` |
| Main model | `anthropic-oauth:claude-sonnet-5-5` |
| Extraction model (resolved) | `anthropic-oauth:claude-sonnet-5-5` — **same as main** (`routing.aux_model` blank → `None` → main-model fallback) |
| Answer model (`memory_retrieval`, no `--model`) | `anthropic-oauth:claude-sonnet-5-5` |
| Embedder (`knowledge.embed_model`) | `qwen3-embedding` |
| Cohort (default) | 12 users, seed 125, sampled from IDs 126–155 |
| Cases (default) | 40 PV4 + 20 changed-fact |
| Extraction cost (would be) | ~2,400 extraction calls |

## Why this is blocked

Card step 1 and acceptance criterion **r1** require:

> If the extraction model resolves to the main model (blank `routing.aux_model`),
> stop and report instead of running. … **nothing is generated.**

On this operator workstation the default instance config
(`~/.protoagent/default/config/langgraph-config.yaml`) leaves `routing.aux_model`
blank. `graph.agent._resolve_aux_model(config, "")` therefore returns `None`, and
`graph.memory_facts.extract_and_store_facts` falls back to the main model
(`anthropic-oauth:claude-sonnet-5-5`). Running `build_pv4` here would extract all
2,400 chats with the reasoning-grade main model — the exact outcome r1 exists to
prevent — and would freeze fixtures that do not reflect the cheap classification-
grade auxiliary extractor the fixtures are meant to characterize.

Resolution was confirmed with no `PROTOAGENT_INSTANCE` override and with a clean
environment; `~/.protoagent/host-config.yaml` sets no `aux_model` either. The
fallback is genuine, not a missing-env artifact.

Because the guard fired, **no** `pv4/cases.jsonl`, **no** `pv4/user_{id}.jsonl`,
and **no** retrieval report were produced. No PV4 case, question, or assertion was
touched.

## How to unblock (for the next operator run)

1. Set a cheap/fast classification-grade alias in the instance config, e.g.
   `routing.aux_model: <cheap-alias>` in
   `~/.protoagent/default/config/langgraph-config.yaml` (host-scoped per ADR 0047),
   **or** pass `--model <alias>` to the builder to override `routing.aux_model` for
   extraction only.
2. Confirm resolution is now distinct from the main model:
   `python -c "from graph.agent import _resolve_aux_model; ..."` (or just re-run the
   builder — it prints the extraction call count before extracting).
3. Generate the frozen fixtures:
   `python -m evals.memory_fixtures.build_pv4 --yes`
   Review the per-user stored-row counts the builder prints and flag any
   `(no durable facts — check the extraction model)` user before accepting.
4. Record the baseline:
   `python -m evals.memory_retrieval --arms topk --runs 3 --json memory-retrieval-pv4-baseline.json`
5. Replace this file with the real baseline. It must record: date, git sha,
   extraction model, answer model, embedder, per-store live/superseded row counts,
   the topk pass counts **per run** for the 40 PV4 cases, the 20 changed-fact cases
   and overall, and the **per-case** pass count (x/3) for both suites. Any case that
   passes in 0 of 3 runs on **both** scorers is a candidate harness bug — note it
   here without editing the case.

## Frozen-fixture policy reminder

Never reword a case prompt to name the expected tool, tell the agent to delegate or
avoid tools, or loosen assertions to obtain a pass. A low baseline is the expected,
useful result; model failures are findings to report or tier as `tracked`.
