#!/usr/bin/env bash
#
# PR gate: require a changelog entry in every PR, so release notes stop depending on
# someone reconstructing a cycle's worth of merges.
#
# The entry belongs in changelog.d/<issue>.<kind>.md (#2322) — a NEW FILE per PR, which
# cannot conflict. Editing CHANGELOG.md directly is still accepted so this change breaks
# no in-flight or fork PR, but it is the path that costs: every such PR writes to the
# same three lines under [Unreleased], so two in flight conflict by construction. Invoked by the `changelog` job in
# .github/workflows/checks.yml; kept as a script so tests/test_changelog_gate.py
# can exercise it against throwaway git repos.
#
#   changelog_gate.sh <base-ref>     # e.g. origin/main
#
# The diff is merge-base based (base...HEAD), so a CHANGELOG entry that landed
# on the base branch after this PR forked does NOT count — the PR itself must
# touch the file.
#
# Escape hatches (any one skips the gate, exit 0):
#   - the PR carries the `skip-changelog` label — read from $GITHUB_EVENT_PATH,
#     the event SNAPSHOT, and this workflow doesn't trigger on `labeled`. Two
#     consequences, both verified the hard way:
#       * `gh pr create --label skip-changelog` does NOT work. GitHub's create-PR
#         API takes no labels, so gh applies them in a second call — after the
#         `opened` event already fired with an empty label set.
#       * A re-run does NOT work either: `gh run rerun` replays that same payload.
#     Only a NEW pull_request event carries the label: a synchronize push (an
#     empty commit is enough) or close+reopen. Label first, then push.
#   - PR_HEAD_REF matches release/* (release PRs roll [Unreleased] themselves)
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
  release/*)
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
if git diff --name-only "${base}...HEAD" \
   | grep -E '^changelog\.d/.+\.md$' \
   | grep -qvx 'changelog.d/README.md'; then
  echo "ok: changelog.d/ fragment added in this PR"
  exit 0
fi

# Editing CHANGELOG.md directly still passes. Kept deliberately: this must not break an
# in-flight or fork PR written against the old convention, and a flag day would fail
# honest PRs for a reason that has nothing to do with their content. Tightening to
# fragments-only is a one-line change once open PRs have cycled.
if git diff --name-only "${base}...HEAD" | grep -qx 'CHANGELOG.md'; then
  echo "ok: CHANGELOG.md touched in this PR (prefer a changelog.d/ fragment — see changelog.d/README.md)"
  exit 0
fi

echo "::error::Missing changelog entry — add changelog.d/<issue>.<kind>.md (see changelog.d/README.md), or apply the skip-changelog label."
exit 1
