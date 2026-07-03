---
name: code-review
description: >-
  /code-review — review a diff along two independent axes, Standards (does it
  follow the repo's conventions?) and Spec (does it do what was asked?), in
  parallel subagents whose findings are never merged.
user_only: true
slash: code-review
---

# Code Review — two axes

A change can follow every convention and still build the wrong thing, or do
exactly what was asked while trashing the codebase's rules. Review both axes
**separately** so neither masks the other.

## 1. Pin the diff

Get a concrete diff before anything else:

- With shell/git access: `git diff <fixed-point>...HEAD` (three-dot, against
  the merge-base). Confirm the ref resolves and the diff is non-empty — a bad
  ref should fail here, not inside two subagents.
- Without git access: ask the user to paste the diff or point at a PR you can
  fetch with the tools you have.

## 2. Find the two source documents

- **Spec source** — the originating ask: a tasks-board entry, an active goal,
  an issue/PRD the user names, or the conversation itself. If none exists,
  the Spec axis reports "no spec available" instead of inventing one.
- **Standards sources** — whatever the repo documents about how code should
  be written (contributor docs, agent instructions, ADRs near the touched
  code). On top of these, the Standards axis always carries a smell baseline:
  mysterious names, duplicated logic, data clumps, primitive obsession,
  shotgun surgery, speculative generality, message chains. Documented repo
  standards override the baseline; baseline hits are judgement calls, never
  hard violations; skip anything tooling already enforces.

## 3. Run both axes in parallel

One `task_batch`, two subagents, each given the diff plus only its own source
documents:

- **Standards brief**: report every documented-standard violation (cite the
  rule) and any baseline smell (name it, quote the hunk), distinguishing hard
  violations from judgement calls. Under 400 words.
- **Spec brief**: report (a) requirements missing or partial, (b) behavior
  nobody asked for, (c) requirements that look implemented but wrong —
  quoting the spec line for each. Under 400 words.

## 4. Aggregate without merging

Present the two reports under `## Standards` and `## Spec`, lightly cleaned.
Do **not** merge or rerank findings across axes — a clean axis must be
visibly clean, and a noisy one must not bury the other. Close with one line
per axis: finding count and the worst item within that axis.

*(Adapted from mattpocock/skills `code-review`, MIT.)*
