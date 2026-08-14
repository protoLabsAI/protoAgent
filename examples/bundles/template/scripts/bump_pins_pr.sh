#!/usr/bin/env bash
# The `bump` job's PR-lifecycle logic (ADR 0049, #2645), extracted out of the
# verify-bundle.yml workflow into a real, unit-testable script — see #2669's
# "commit the stubbed git/gh dry-run test harness" work item. Previously this
# logic only lived inline in YAML `run:` blocks, so verifying a change to it
# meant a manual dry-run against a real GitHub repo. tests/test_bump_pins_pr.py
# drives this file directly, with `gh` stubbed on PATH and a real throwaway git
# repo — see that file's docstring for the harness shape.
#
# Two subcommands, mirroring the workflow's two `bump`-job steps:
#   open-or-update            — reuse the ONE `bump-pins` branch/PR per stack (dedup;
#                                #2645) instead of piling up a dated branch every run.
#   flag-approval <number> <branch> <sha>
#                              — GitHub never auto-starts a `pull_request` run for a
#                                PR opened with GITHUB_TOKEN (recursion-prevention);
#                                make that stall visible (label + comment + red job)
#                                instead of letting the candidate rot silently.
#
# Every output this prints as `key=value` on stdout ALSO goes to $GITHUB_OUTPUT when
# that's set (real CI) — callers (CI or a test) only ever need to read stdout.
set -euo pipefail

# Scratch dir for the two hand-off files below — /tmp in real CI (one runner per job, no
# collision risk); overridable so parallel test processes don't share a literal /tmp path.
SCRATCH="${BUMP_PINS_SCRATCH_DIR:-/tmp}"

emit() {
  # emit KEY VALUE — stdout always; $GITHUB_OUTPUT too, when the caller set one.
  echo "$1=$2"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1=$2" >>"$GITHUB_OUTPUT"
  fi
}

cmd_open_or_update() {
  local branch="bump-pins"
  git config user.name "bundle-bot"
  git config user.email "bundle-bot@users.noreply.github.com"
  git checkout -b "$branch"
  git commit -am "chore: bump plugin pins to latest release tags"
  git push --force origin "$branch"
  emit sha "$(git rev-parse HEAD)"

  # (A heredoc nested inside a `$(...)` command substitution mis-parses any
  # apostrophe in its body under bash — write straight to a file instead.)
  cat >"$SCRATCH/pr_body.md" <<BODY
Automated pin bump (ADR 0049).

\`\`\`
$(cat "$SCRATCH/bumps.txt" 2>/dev/null || true)
\`\`\`

Merging requires the verify job green — the pin only moves through a passing verification.

This PR was opened with the repository GITHUB_TOKEN, so GitHub holds its \`verify\` run for approval (Actions tab -> **Approve and run workflows**) instead of starting it automatically — the documented explicit-approval contract, see README.md's "Pin-bump PR lifecycle" section (#2645). A later scheduled run updates THIS PR in place rather than opening a new one.
BODY

  local number
  number="$(gh pr list --state open --head "$branch" --json number --jq '.[0].number // empty')"
  if [ -n "$number" ]; then
    echo "updating existing pin-bump PR #$number"
    gh pr edit "$number" --body-file "$SCRATCH/pr_body.md"
  else
    gh pr create --title "chore: bump plugin pins to latest release tags" --body-file "$SCRATCH/pr_body.md"
    number="$(gh pr list --state open --head "$branch" --json number --jq '.[0].number')"
    echo "opened pin-bump PR #$number"
  fi
  emit number "$number"
  emit branch "$branch"
}

cmd_flag_approval() {
  local number="$1" branch="$2" sha="$3"
  local status="" conclusion="" run_url=""

  local workflow_file="${BUMP_PINS_WORKFLOW_FILE:-verify-bundle.yml}"
  for _ in 1 2 3 4 5 6; do
    run_json="$(gh run list --branch "$branch" --commit "$sha" --event pull_request \
      --workflow "$workflow_file" --limit 1 \
      --json status,conclusion,url 2>/dev/null || echo '[]')"
    status="$(echo "$run_json" | jq -r '.[0].status // empty')"
    conclusion="$(echo "$run_json" | jq -r '.[0].conclusion // empty')"
    run_url="$(echo "$run_json" | jq -r '.[0].url // empty')"
    if [ -n "$status" ]; then
      break
    fi
    sleep "${BUMP_PINS_POLL_INTERVAL:-10}"
  done

  if [ "$conclusion" = "action_required" ] || [ -z "$status" ]; then
    local why
    if [ -z "$status" ]; then
      why="its verify run never showed up in the run list at all (even more silent than the known action_required stall)"
    else
      why="its verify run concluded action_required with no jobs started"
    fi
    local msg="Pin-bump PR #$number needs a maintainer to approve its verify run — $why. GITHUB_TOKEN-authored PRs don't run \`pull_request\` workflows automatically; click **Approve and run workflows** on the Actions tab${run_url:+ ($run_url)}. This is the documented explicit-approval step (README.md \"Pin-bump PR lifecycle\", #2645) — approve it (or close the PR) so the pin doesn't sit unverified."
    echo "::error::$msg"

    gh label create "needs-approval" --color FBCA04 \
      --description "A workflow run is stuck action_required and needs a maintainer to approve it" \
      2>/dev/null || true
    gh pr edit "$number" --add-label "needs-approval" || true

    local already
    already="$(gh pr view "$number" --json comments --jq \
      '[.comments[].body | select(contains("Approve and run workflows"))] | length' \
      2>/dev/null || echo 0)"
    if [ "${already:-0}" -eq 0 ]; then
      gh pr comment "$number" --body "$msg" || true
    fi

    exit 1
  fi

  echo "verify run started without manual approval (status=$status)"
}

case "${1:-}" in
open-or-update)
  cmd_open_or_update
  ;;
flag-approval)
  cmd_flag_approval "$2" "$3" "$4"
  ;;
*)
  echo "usage: $0 open-or-update | flag-approval <pr-number> <branch> <sha>" >&2
  exit 2
  ;;
esac
