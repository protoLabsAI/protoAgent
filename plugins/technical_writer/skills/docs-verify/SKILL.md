---
name: docs-verify
description: Use after documentation changes to check accuracy, links, generated content, builds, review, and authorized publication or merge.
---

# Verify and deliver

1. Review the diff against the requested scope and repository rules. Preserve unrelated
   work. Confirm generated output was changed at its source, then regenerated. Recheck
   changed factual claims against the target version and source references.
2. Run the repo-required gates plus checks appropriate to the change: docs build,
   generation drift, links/fragments, preserved anchors, examples, navigation, media,
   or UI preview. Inspect failures; distinguish baseline failures from regressions.
   Do not write tests that merely assert arbitrary prose or count words.
3. Exercise the changed reader journey in an isolated environment where feasible.
   Report separately: source inspection, command execution, static build, browser,
   native shell, CI, installed package, and deployment. None proves all the others.
4. If delivery is authorized, commit only scoped files, open/update the PR using the
   repo's template, add release notes where required, and follow its review/merge rules.
   Read current-head reviews and unresolved threads, including warnings in a green gate.
   Resolve valid findings and rerun affected checks. Do not bypass gates to finish sooner.
5. Merge/publish only within the operator's existing authorization. When authorized,
   continue through required checks and review instead of returning merely a draft.
   Verify the deployed revision if deployment is part of scope. Update the ledger and
   issue body; close a tracker only when all its acceptance criteria are complete.

Done when the authorized delivery stage is reached with evidence, or a concrete blocker
is recorded. Report changed behavior/content, validation actually performed, limitations,
and any remaining work. Infrastructure delivered and media production finished are
separate outcomes; likewise merged and deployed.
