---
name: docs-audit
description: Use when auditing docs section by section for stale claims, missing tasks, verbosity, complexity, redundancy, or historical material.
---

# Audit systematically

Use the repository's existing tracker; otherwise keep a compact ledger near contributor
notes. Inventory all pages in scope from navigation and the filesystem, including
unlinked pages. Record the baseline commit and distinguish screened pages from pages
whose behavior was deeply verified. Do not claim a complete audit from a sidebar scan.

For each section, read its content and representative source before editing. Record:

| Field | Required evidence |
| --- | --- |
| Reader/job | Who uses the section and what they must accomplish |
| Coverage | Pages read, journeys traced, source paths and target version |
| Findings | Incorrect/stale, missing, duplicate, overloaded, misplaced, or historical |
| Disposition | Keep, revise, consolidate, split, move, archive, or remove; with rationale |
| Task | Specific change, priority, dependencies, acceptance check, status, delivery link |

Prioritize broken onboarding, incorrect commands, unsafe missing prerequisites, and
blocked tasks before editorial polish. Brevity is not a word-count target: remove
irrelevance and duplication while retaining the information needed to succeed.
Distinguish a documentation defect from a product defect; do not silently change runtime
behavior to make a sentence true. Track product defects separately within authorization.

Execute approved edits one coherent section or dependency group at a time. Preserve
canonical links and useful anchors when consolidating. Date and label completed plans
or superseded guidance; do not rewrite historical decisions as current instructions.
After each slice, run `docs-verify`, update the ledger, and continue without requesting
the same approval again. Keep deferred tasks explicit rather than hiding them in prose.

Done when every scoped page has a recorded disposition, accepted tasks have verified
changes or a specific blocker, and the remaining backlog names the exact uncompleted work.
