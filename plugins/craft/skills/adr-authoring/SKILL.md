---
name: adr-authoring
description: >-
  Use this when writing, drafting, or updating an Architecture Decision Record
  (ADR) or a plan document for this repo — e.g. "write an ADR for X", "record
  this decision", "author the ADR for this design", "draft the plan doc".
  Covers the house MADR shape, numbering, the index row, the docs-nav
  regeneration step, and the VitePress traps that fail the docs build.
---

# Authoring ADRs (and plan docs)

An ADR that doesn't pass the docs build or drifts from the house shape costs a
review round-trip. This is the checklist that makes an agent-authored ADR land
first try.

## 1. Number and file

- Next number = highest existing `docs/adr/NNNN-*.md` + 1, zero-padded to 4.
  Check right before opening the PR — another PR may have taken your number.
- Filename: `docs/adr/NNNN-short-kebab-slug.md`. Never renumber or delete a
  merged ADR — supersede it (new ADR, and flip the old one's Status to
  `Superseded by [NNNN](./NNNN-….md)`).

## 2. The house MADR shape

```markdown
# NNNN — Title: what is decided, not the topic area

- Status: Proposed            ← Proposed | Accepted | Superseded by NNNN
- Date: YYYY-MM-DD
- Builds on: ADR 00XX (one-line why), ADR 00YY (…)   ← the lineage, load-bearing
- Plan: [`docs/plans/<slug>.md`](../plans/<slug>.md)  ← if a plan doc exists

## Context
## Decision
## Consequences
```

- **Builds on** is not decoration — reviewers navigate the decision graph
  through it. Cite the ADRs whose machinery you extend or constrain, each with
  a clause saying *how* it relates.
- **Decision** carries labeled sub-decisions (`**D1 — Name.**` …) when there is
  more than one; consumers cite "ADR NNNN D3" from code comments.
- **Consequences** includes the honest costs and the revisit trigger, not just
  benefits.
- Write in the repo's voice: dense, specific, mechanism-first. Name files,
  fields, and functions in backticks. No filler sections (no empty
  "Alternatives considered" — fold real alternatives into Context or Decision).

## 3. The index row

Add one row to the table in `docs/adr/index.md`, after the previous highest:

```markdown
| [NNNN](./NNNN-slug.md) | Title — a summary dense enough to decide "do I need to read this?" from the index alone | Proposed |
```

- **One physical line** — never wrap the row.
- Escape any `|` inside the summary as `\|`.

## 4. Regenerate the docs nav

```
python scripts/gen_docs_nav.py
```

ADRs are enumerated from the filesystem into `plugins/docs/nav.json`, and
`tests/test_docs_plugin.py` fails CI when the committed file is stale. A new
ADR file without a nav regen is a red build. Commit the regenerated
`plugins/docs/nav.json` with the ADR.

## 5. VitePress traps (each has failed a real build)

- **Every relative link must resolve within this PR.** The dead-link check
  fails the build on a link to a file that isn't on main *and* isn't added by
  your PR — a plan doc still sitting in an unmerged PR is a dead link until it
  merges. Either merge the target first or hold the link until it exists.
- Relative links from `docs/adr/` go up one level: `../plans/<slug>.md`,
  `../guides/<slug>.md`. Link the `.md` path (VitePress rewrites it).
- **No wrapped code spans**: a backtick span broken across lines renders as
  literal backticks and can break table rows.
- Bare `<angle-bracket>` tokens outside code spans are parsed as HTML tags and
  can blank out the rest of the page — backtick them.

## 6. Plan docs (`docs/plans/`)

Same discipline, different genre: a plan is *phased work* (milestones, sizes,
dependency spine, accept criteria per milestone), where an ADR is *one
decision*. Plans live at `docs/plans/<slug>.md`, get linked from the ADRs they
produced (`- Plan:` header line) and link back to the ADRs they cite. A plan
that changes a decision doesn't edit the ADR's Decision section — it spawns a
new ADR.

## 7. Verify before the PR

```
python scripts/gen_docs_nav.py --check   # nav in sync
npm run docs:build                       # dead links + markdown traps
python -m pytest tests/test_docs_plugin.py -q
```

`npm run docs:build` is the one that catches the link/markdown traps — run it
even for a "one-line" ADR edit.
