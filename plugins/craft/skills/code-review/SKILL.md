---
name: code-review
description: >-
  /code-review — adversarial PR review via the code-review workflow: four
  parallel finder angles, dedup, an evidence-checking verify pass, and a
  findings report. Falls back to a two-axis inline review when the workflow
  isn't available.
user_only: true
slash: code-review
---

# Code Review — adversarial panel

Drive the `code-review` workflow (ADR 0077): four review-finders read the diff
in parallel (correctness / removed behavior / cross-file / conventions), a
synthesizer dedups and ranks, a verifier re-reads the diff and
confirms/refutes each finding, and the refuted ones are dropped. You gather
the inputs, run it, and present the outcome — you do not review inline.

## 1. Pin the PR

The workflow reviews a **pull request** (it reads via `github_pr_diff`):

- Given a PR number or URL → extract the number, and the `owner/name` repo if
  it differs from the configured default.
- Given a branch or "my current changes" → find its PR (`github_get_pr` /
  `gh pr view`). No PR yet → say so and offer the fallback below.
- Given only a commit SHA → the finders can use `github_get_commit_diff`;
  pass the SHA as `pr` only if there is no PR, and say the verify pass will
  anchor to the commit.

## 2. Run the workflow

```
run_workflow("code-review", {"pr": "<number>", "repo": "<owner/name or omit>"})
```

It is slow-ish (6 subagent steps, 4 in parallel) — tell the user it's running.

## 3. Present the outcome

The output ends with the canonical fenced findings JSON
(`graph/review/findings.py` schema — file, line, severity, category, claim,
evidence, verdict). Present the prose brief, then the findings **grouped by
severity, worst first**, keeping each finding's verdict visible ("confirmed"
vs "uncertain" — the reader weighs them differently). A clean review is a
result, not a failure: say the panel found nothing and name the angles it
covered. Don't re-litigate individual findings — the verify pass already did;
if the user disputes one, offer to re-run the verifier on just that claim.

## Fallback — workflow unavailable

If `run_workflow` or the github tools aren't available (plugin disabled, no
`gh` auth), run the lightweight two-axis review inline instead: pin a concrete
diff (`git diff <fixed-point>...HEAD`, three-dot), then two parallel
delegations (`subagent_type: verifier`) — a **Standards** brief (documented
repo conventions + baseline smells: mysterious names, duplicated logic, data
clumps, shotgun surgery) and a **Spec** brief (missing/partial requirements,
unrequested behavior, implemented-but-wrong) — and present the two reports
under `## Standards` / `## Spec` without merging them, one closing line per
axis. *(Adapted from mattpocock/skills `code-review`, MIT.)*
