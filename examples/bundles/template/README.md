# Bundle template — the pin lifecycle baked in (ADR 0049)

A reference layout for a protoAgent **plugin bundle** repo whose pins stay honest. A
bundle (ADR 0040) is a reference manifest over standalone plugin repos — its whole value
is "this combo was verified together". This template adds the lifecycle that keeps that
claim true after authoring day:

| File | Role |
|---|---|
| `protoagent.bundle.yaml` | The manifest — tag pins + `verified_against:` (the rules are commented inline) |
| `scripts/verify_bundle.py` | Installs the pin set into a scratch agent, loads every member, probes every declared console view for 200 |
| `scripts/check_bundle_updates.py` | Rewrites tag pins to the newest release tag (comment-preserving) |
| `.github/workflows/verify-bundle.yml` | Wires both into CI: verify on every PR + weekly; open/update ONE pin-bump PR on schedule, manual dispatch, or a member's `member-released` dispatch |

## The core rules

1. **Pin release tags, not raw SHAs.** Legible, advisable (the console's update check can
   ls-remote a tag), and it nudges plugin repos to cut releases. The installer still locks
   the exact commit SHA — reproducibility is unchanged.
2. **Record `verified_against:`** — the core version this pin set was last verified on.
3. **A pin only moves through a passing verification.** Hand-edit or bot-bump, either way
   the PR must pass `verify` — which installs the manifest for real and probes every
   declared view. The weekly schedule re-runs it so silent rot turns a badge red.

## Pin-bump PR lifecycle (explicit-approval model, [#2645][issue-2645])

The scheduled `bump` job pushes to a single, stable branch — `bump-pins` — and keeps **at
most one** open pin-bump PR per bundle repo (one manifest, one candidate). A
later scheduled run that finds more bumps force-pushes that same branch, updating the PR
in place instead of piling up duplicates. Treat `bump-pins` as bot-owned: it's rewritten
wholesale every run, so hand edits to it don't survive the next bump.

GitHub does **not** auto-start a `pull_request` workflow run for a PR opened with the
repository `GITHUB_TOKEN` — it's held `action_required` until someone with write access
clicks **Approve and run workflows** on the Actions tab (recursion-prevention; see
[GitHub's docs][gh-token-docs]). This template has no GitHub App installation or PAT
provisioned to avoid that (the alternative, Option 1 in #2645, needs org-level credentials
this repo doesn't have), so it deliberately runs the **explicit-approval model** instead:

- **Approving is a documented, one-click operator responsibility, not a bug.** Watch the
  repo's Actions tab (or PR notifications) for the pin-bump PR and approve its run so
  `verify` actually runs before merge.
- **The `bump` job makes a stall visible instead of silent.** After pushing, it polls
  (bounded wait) for the `verify` run it should have queued. If that run comes back
  `action_required` — or never shows up at all, which is worse — the job **fails**,
  comments on the PR, and adds a `needs-approval` label. An unapproved pin-bump PR then
  shows up as a red weekly schedule, not a PR quietly rotting for weeks.
- **ADR 0049's invariant still holds either way:** `verify` still has to pass before merge
  — this only makes sure someone notices it needs to be *started*.

If your org does have a GitHub App installation or PAT available, Option 1 (swap
`GH_TOKEN: ${{ github.token }}` in the `bump` job for that token) removes the approval
step entirely and this whole section stops applying.

[issue-2645]: https://github.com/protoLabsAI/protoAgent/issues/2645
[gh-token-docs]: https://docs.github.com/en/actions/concepts/security/github_token#when-github_token-triggers-workflow-runs

**Out of scope here:** this is a template-only change. The four archetype repos published
before this contract existed (`cowork-archetype`, `design-system-archetype`,
`portfolio-manager-archetype`, `product-archetype` — renamed from `*-stack` 2026-08-19)
still run the old duplicate-opening workflow and between them carry 17
open, unverified pin-bump PRs. Migrating each repo's workflow and reconciling that backlog
is separate follow-up work, tracked on #2645 — each is its own repo with its own PR queue,
not something a protoAgent-core PR touches.

## Members poke the bundle on release ([#2960][issue-2960])

On its own, `bump` only notices a new member release at the next scheduled run — a member
can cut four releases in a day and the pins lag until Monday. So `verify-bundle.yml` also
listens for a `repository_dispatch` with event type `member-released` (which passes the
`bump` job's `!= 'pull_request'` gate), and a member plugin repo sends exactly that from
its release workflow the moment a release is published:

```yaml
# In the member repo's release.yml, after the release-creating step —
# full runnable context: ../member-release-notify.yml
- name: Notify archetype repos
  if: steps.v.outputs.exists == 'false'
  env:
    GH_TOKEN: ${{ secrets.GH_PAT || secrets.GITHUB_TOKEN }}
  run: |
    for repo in ${{ inputs.archetype_repos || '' }}; do
      gh api repos/$repo/dispatches -f event_type=member-released -f "client_payload[plugin]=${{ github.repository }}" -f "client_payload[tag]=${{ steps.v.outputs.tag }}" || true
    done
```

`archetype_repos` defaults to empty, so the step no-ops in plugins that aren't in any
archetype; members list their archetypes (e.g. `protoLabsAI/project-manager-archetype`).
The member's repo-scoped `GITHUB_TOKEN` can't dispatch cross-repo, so a real notify needs
a `GH_PAT` secret — without one the `|| true` keeps the member's release green and the
weekly cron still catches up. Best-effort by design: the dispatch is a hint, the schedule
is the backstop.

[issue-2960]: https://github.com/protoLabsAI/protoAgent/issues/2960

## Using it

```bash
# Start a bundle repo from this template
cp -r examples/bundles/template my-archetype && cd my-archetype && git init

# Verify locally (from a protoAgent checkout with deps synced)
uv run --no-sync python /path/to/my-archetype/scripts/verify_bundle.py /path/to/my-archetype

# Check for newer member releases
python3 scripts/check_bundle_updates.py protoagent.bundle.yaml
```

Why this exists: the first real bundle shipped pins that predated both members' view
fixes — every agent spawned from the archetype got 404 panels out of the box, and nothing
flagged it. The verify probe above catches exactly that, at authoring time and weekly
thereafter. Full rationale: [ADR 0049](../../../docs/adr/0049-bundle-pin-lifecycle.md);
the live adopter is [portfolio-manager-archetype](https://github.com/protoLabsAI/portfolio-manager-archetype).
