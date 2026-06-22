# Contributing

protoAgent's full contributor guide — run commands, the **must-pass-before-PR
gates**, and the gotchas that recur — lives in **[PROTO.md](./PROTO.md)**. Read it
before sending code.

This file covers the one thing GitHub surfaces directly on the *New issue* page:
**what a good issue needs.**

## Filing an issue

Use a template — **Bug report** or **Enhancement / feature** — whenever you can.
A template's required fields are exactly what the gate checks, so a template-filed
issue always passes.

Every issue — however it's filed (web, `gh issue create`, or an agent) — should have:

- **A substantive description.** Not just a title; state the actual problem.
- **A Problem / What's-wrong / Motivation section** — *why* it matters, and
  *where* (name the file / subsystem / ADR).
- **Type-specific detail:**
  - *Bug* (`bug` label): **Steps to reproduce / Evidence** and **Expected vs.
    actual**.
  - *Enhancement* (`enhancement` label): a **Proposed direction** and/or
    **Acceptance** criteria.
- **Refs** to related issues / PRs / ADRs where relevant (`#1300`, `ADR 0047`).

See #1159, #1300, #1310 for the house style: Problem → (What's wrong / Proposed
direction) → Acceptance → Refs.

## The issue gate

`.github/workflows/issue-gate.yml` runs on every opened/edited issue and checks
the requirements above. It is **silent** — it never comments. An issue missing
required sections just gets the **`needs-info`** label, nothing else.

- **To clear it:** edit the issue to add the missing sections. The gate re-runs
  on edit and **removes `needs-info`** automatically once the issue conforms.
- **Intentional free-form** (a maintainer tracking note, a quick agent split-out):
  add the **`gate-exempt`** label and the gate skips the issue.

No required field blocks you from *opening* an issue — the gate only flags, it
never closes.
