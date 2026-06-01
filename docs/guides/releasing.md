# Releasing

protoAgent releases are **manual and on-demand** — you pick the bump level and
run one workflow when a batch of work is ready. Merges to `main` do **not** cut
releases on their own.

## The flow at a glance

```
feature PR (adds a CHANGELOG [Unreleased] entry)  ──▶  merge to main
                                                          │
run "Prepare Release" (workflow_dispatch, pick bump) ◀────┘
   │  bumps pyproject.toml + rolls CHANGELOG.md
   │  opens chore: release vX.Y.Z PR → auto-merges when the 3 checks pass
   ▼
pushes tag vX.Y.Z  ──▶  Release workflow:
                          • builds + pushes the semver Docker tags
                          • creates the GitHub Release (notes minus chore/docs)
                          • posts notes to Discord (release-tools)
```

`latest` Docker tag is pushed on every `main` merge by `docker-publish.yml` —
independent of releases.

## Cutting a release

1. **Actions → Prepare Release → Run workflow.**
2. Choose the **bump**: `patch` (default) · `minor` · `major`. Use `dry_run`
   to preview the version + changelog/​pyproject diff without opening a PR.
3. The workflow bumps the version, rolls the changelog, opens
   `chore: release vX.Y.Z`, and auto-merges once the required checks pass, then
   tags `vX.Y.Z` — which triggers the **Release** workflow.

That's it. Don't bump `pyproject.toml` or tag by hand — the workflow owns both.

## The changelog protocol

We keep a [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)-style
[`CHANGELOG.md`](https://github.com/protoLabsAI/protoAgent/blob/main/CHANGELOG.md).

- **In your feature PR**, add a bullet under `## [Unreleased]` in the right
  group (`### Added` / `### Changed` / `### Fixed` / `### Removed` / `### Docs`).
- **At release time**, `scripts/changelog.py roll <version>` (run by
  `prepare-release.yml`) moves everything under `[Unreleased]` into a dated
  `## [X.Y.Z] - YYYY-MM-DD` section and leaves a fresh empty `[Unreleased]`.
- The rolled changelog is committed **inside the release PR**, so it goes
  through the same `main` ruleset (PR + checks) as any change — nothing is
  pushed to `main` directly.

## Branch protection

`main` is protected by a repository **ruleset**: every change needs a PR, and
the three CI checks must pass to merge —

| Check | Workflow |
|---|---|
| Verify workspace config | `checks.yml` (runs `release-tools`' `verify-workspace-config`) |
| Python tests | `checks.yml` (`pytest`) |
| Web E2E smoke | `checks.yml` (Playwright vs. mock backend) |

Direct pushes, force-pushes, and branch deletion are blocked. Approvals are set
to **0** so the solo/automated flow (you + the release bot) is never blocked on
a reviewer — the gate is CI, not human review.

## Required secrets

| Secret | Used by | Purpose |
|---|---|---|
| `GH_PAT` | `prepare-release.yml` | A PAT (not `GITHUB_TOKEN`) so the tag push can trigger the downstream Release workflow. |
| `GATEWAY_API_KEY` | `release.yml` (release-tools) | Rewrites the commit range into themed release notes via the protoLabs gateway. |
| `DISCORD_RELEASE_WEBHOOK` | `release.yml` (release-tools) | Posts the release embed to Discord. **Optional** — the step is `continue-on-error`, so releases still succeed without it; set it to enable the Discord post. |
