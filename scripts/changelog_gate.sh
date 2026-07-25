#!/usr/bin/env bash
#
# PR gate: require a CHANGELOG.md change in every PR (the [Unreleased] entry
# PROTO.md asks for), so release notes stop depending on someone reconstructing
# a cycle's worth of merges. Invoked by the `changelog` job in
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

if git diff --name-only "${base}...HEAD" | grep -qx 'CHANGELOG.md'; then
  echo "ok: CHANGELOG.md touched in this PR"
  exit 0
fi

echo "::error::Missing changelog entry — add an entry under [Unreleased] in CHANGELOG.md or apply the skip-changelog label."
exit 1
