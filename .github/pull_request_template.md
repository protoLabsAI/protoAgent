<!--
Keep this short. PR body is read by three audiences:
  1. CodeRabbit (context for review)
  2. `gh api releases/generate-notes` (shows up in every release)
  3. Whoever debugs this PR in six months

Anything irrelevant to one of those can go in the Slack thread / commit body.
-->

## Summary

<!-- 1–3 bullets. What changed, why. Not "what files" — that's in the diff. -->

-

## Closes

<!--
GitHub auto-closes linked issues when you use these exact keywords here:

    Closes #123
    Fixes #456
    Resolves #789

Plain references ("see #123" or "related to #456") DO NOT auto-close. If
this PR ships a fix for an open issue, put it here so the issue moves on
merge. Leave blank if this PR doesn't close anything.
-->

Closes #

## Test plan

<!-- Bulleted checklist of how you verified. -->

- [ ]

## Checklist

<!-- The `changelog` CI job enforces the first item — a PR that touches neither
CHANGELOG.md nor an escape hatch (skip-changelog label, release/* branch,
dependabot) fails checks. -->

- [ ] CHANGELOG.md has an entry under `[Unreleased]`, or the `skip-changelog` label is applied (and justified)
- [ ] A `Fixes #N` / `Closes #N` line above points at the issue this PR ships
