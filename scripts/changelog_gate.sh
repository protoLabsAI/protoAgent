#!/usr/bin/env bash
#
# PR gate: require a changelog entry in every PR, so release notes stop depending on
# someone reconstructing a cycle's worth of merges.
#
# The entry belongs in changelog.d/<issue>.<kind>.md (#2322) — a NEW FILE per PR, which
# cannot conflict. A direct CHANGELOG.md edit does NOT satisfy this gate: every such PR
# writes to the same three lines under [Unreleased], so two in flight conflict by
# construction (a 13-PR stack cost ~10 extra CI cycles to that one anchor). CHANGELOG.md
# is written by the release process — `changelog.py collate` — not by feature PRs. Invoked by the `changelog` job in
# .github/workflows/changelog.yml — its OWN workflow since #2293, not checks.yml, so that
# labeling can re-trigger it without re-running the whole suite; kept as a script so
# tests/test_changelog_gate.py can exercise it against throwaway git repos.
#
#   changelog_gate.sh <base-ref>     # e.g. origin/main
#
# The diff is merge-base based (base...HEAD), so a CHANGELOG entry that landed
# on the base branch after this PR forked does NOT count — the PR itself must
# touch the file.
#
# Escape hatches (any one skips the gate, exit 0):
#   - the PR carries the `skip-changelog` label. Just apply it — since #2293 the
#     changelog workflow triggers on `labeled`/`unlabeled`, so labeling fires a fresh
#     pull_request event whose payload carries the label and the gate re-runs itself.
#     No empty commit, no close/reopen.
#     The mechanism is worth knowing, because it constrains the fix: the label is read
#     from $GITHUB_EVENT_PATH, the event SNAPSHOT, so it has to be on the PR when the
#     run STARTS. Two shortcuts therefore still don't work, and never will —
#       * `gh pr create --label skip-changelog` doesn't get you a labeled `opened`
#         event: GitHub's create-PR API takes no labels, so gh applies them in a
#         second call, after `opened` already fired with an empty label set.
#       * `gh run rerun` replays that same payload by definition.
#     Neither matters now: both leave a red run that the `labeled` event supersedes.
#   - PR_HEAD_REF matches release/* or prepare-release/* (release PRs roll [Unreleased]
#     themselves; prepare-release.yml creates the latter spelling)
#   - PR_ACTOR is dependabot[bot] (bot PRs never need entries)
#
# Pure git + jq + shell — no dependency install, safe to run first in CI.
set -euo pipefail

base="${1:?usage: changelog_gate.sh <base-ref>}"

if [ "${PR_ACTOR:-}" = "dependabot[bot]" ]; then
  echo "skip: dependabot PR — no changelog entry required"
  exit 0
fi

case "${PR_HEAD_REF:-}" in
  # prepare-release.yml creates `prepare-release/vX.Y.Z`, which `release/*` does NOT
  # match. That went unnoticed because a release PR *deletes* every fragment, and the
  # old check counted any changed changelog.d path — including deletions — so those PRs
  # passed by accident. #2600 stopped counting deletions (removing someone else's entry
  # isn't self-documentation) and the accident stopped covering for the pattern, which
  # broke the v0.134.0 release PR. Match both spellings.
  release/*|prepare-release/*)
    echo "skip: release branch '${PR_HEAD_REF}' rolls [Unreleased] itself"
    exit 0
    ;;
esac

if [ -n "${GITHUB_EVENT_PATH:-}" ] && [ -f "${GITHUB_EVENT_PATH}" ]; then
  if jq -e '.pull_request.labels // [] | any(.name == "skip-changelog")' \
       "${GITHUB_EVENT_PATH}" >/dev/null; then
    echo "skip: skip-changelog label present"
    exit 0
  fi
fi

# A news fragment is the expected path (#2322): distinct filenames never conflict, so a
# stack of PRs no longer costs one serial merge per PR to a single shared anchor.
# At least one changelog.d/*.md that ISN'T the README — a PR may legitimately touch the
# README *and* add a fragment, so this filters the README out rather than disqualifying
# the whole PR when it appears (which is what an earlier version of this check did).
# --diff-filter=d excludes DELETIONS: removing someone else's fragment is not "this PR
# documented itself", and a deleted path can't be linted below either.
fragments=$(git diff --name-only --diff-filter=d "${base}...HEAD" \
   | grep -E '^changelog\.d/.+\.md$' \
   | grep -vx 'changelog.d/README.md' || true)

if [ -n "${fragments}" ]; then
  # Existing is not the property that matters — SURVIVING COLLATION is (#2600). A fragment
  # whose text isn't a top-level `- ` bullet passes an existence check, then contributes
  # nothing to the release notes, and a release with no parsable bullets drops off
  # /changelog entirely. That only shows up days later to whoever cuts the release, so
  # check it here with the collator's OWN parser rather than a second guess at the rules.
  #
  # Resolved from this script's directory, not $PWD: the tests drive the gate against
  # throwaway repos. python3 + stdlib only, so the gate still installs nothing.
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # shellcheck disable=SC2086 # word-splitting is intended: one arg per fragment path
  if ! python3 "${here}/changelog.py" lint-fragments ${fragments}; then
    echo "::error::A changelog fragment in this PR would be dropped from the release notes. Fix it as printed above (changelog.d/README.md has the format), or apply the skip-changelog label."
    exit 1
  fi
  echo "ok: changelog.d/ fragment added in this PR"
  exit 0
fi

# A direct CHANGELOG.md edit NO LONGER counts. The transitional acceptance existed only
# so #2322 wouldn't fail PRs already open against the old convention; it was retired once
# the queue drained to zero, so nothing is grandfathered and nothing broke. Editing
# CHANGELOG.md isn't forbidden (an old section may need a typo fix) — it just doesn't
# satisfy the gate, because an entry written there reintroduces the shared-anchor
# conflict this whole change exists to remove.
echo "::error::Missing changelog entry — add changelog.d/<issue>.<kind>.md (see changelog.d/README.md), or apply the skip-changelog label. Editing CHANGELOG.md directly no longer satisfies this gate: entries there conflict between PRs (#2322)."
exit 1
