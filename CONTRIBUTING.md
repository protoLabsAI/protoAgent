# Contributing

protoAgent's full contributor guide — run commands, the **must-pass-before-PR
gates**, and the gotchas that recur — lives in **[PROTO.md](./PROTO.md)**. Read it
before sending code.

By participating in this project you agree to abide by the
[Code of Conduct](./CODE_OF_CONDUCT.md).

This file covers what GitHub surfaces directly on the *New issue* and *New pull
request* pages: **what a good issue needs**, and the three PR habits that keep
an external PR mergeable.

## Sending a pull request

The gates and gotchas live in [PROTO.md](./PROTO.md) — `python scripts/gate.py`
runs the fast gate (lint + unit tests, the same script CI invokes) in one
command; the "Must pass before opening a PR" table there has the full
breakdown. Beyond green checks:

1. **Add a changelog fragment** — `changelog.d/<pr>.<kind>.md`, a markdown
   bullet with a **bold lead-in** ending in `(#NNNN)`. Shape and kinds:
   [changelog.d/README.md](./changelog.d/README.md). The `Changelog entry` check
   fails without one (or the `skip-changelog` label for CI/test-only changes).
2. **Tick "Allow edits by maintainers"** so we can push small fixups (a
   fragment, a review nit) to your branch instead of superseding your PR.
3. **Base on current `main`** — the suite and gates move fast here.

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
