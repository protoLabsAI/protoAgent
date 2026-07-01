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
   │  opens chore: release vX.Y.Z PR   (does NOT merge or tag)
   ▼
you merge the PR (CI green)  ──▶  you push tag vX.Y.Z  ──▶  Release workflow (on: push tag):
                                                              • builds + pushes the semver Docker tags
                                                              • creates the GitHub Release (notes minus chore/docs)
                                                              • posts notes to Discord (release-tools)
```

`latest` Docker tag is pushed on every `main` merge by `docker-publish.yml` —
independent of releases.

### Desktop

Desktop builds are **manual** — `desktop-build.yml` runs on `workflow_dispatch` only,
**not** on tag pushes. The macOS (10×) and Windows (2×) legs are the repo's only paid
CI, and building the full matrix on every tag (dozens/month) was the dominant cost, so
desktop drops are on-demand. A normal `git push` of a tag still ships the Docker image
and a GitHub Release (via `release.yml`); only the desktop binaries wait for a dispatch.

To cut a desktop release, dispatch `desktop-build.yml` with the **tag** input set to the
release tag (`vX.Y.Z`):

```sh
gh workflow run desktop-build.yml -f tag=vX.Y.Z
```

That builds the three-platform matrix and attaches the artifacts to that GitHub Release:
the macOS `.dmg` (signed + notarized — requires the full Apple secret set, the leg fails
otherwise), the Linux `.AppImage` + `.deb`, and the Windows NSIS `-setup.exe` (both
unsigned). When the org updater signing key is present, the legs also attach signed
updater bundles and a fan-in job uploads `latest.json` (the manifest the in-app updater
polls) and promotes the release to **Latest**. See `apps/desktop/README.md` §§ Platforms
& CI / Updates.

> **Dispatching without a tag** (from a branch) is a **test build**: bundles upload as
> workflow artifacts only — no release, no `latest.json`, no `Latest` change.

> **`Latest` tracks the last desktop release, not the newest tag.** `release.yml` creates
> every release `--latest=false`; a release is promoted to `Latest` only when its desktop
> build's fan-in has uploaded `latest.json`, so the in-app updater never 404s on a release
> that has no manifest. Tags you never build desktop for stay non-Latest (their Docker
> image and notes are still published).

> **Runner cost.** Every other workflow runs on Namespace; the desktop matrix is
> the only GitHub-hosted usage (macOS bills at 10×, Windows 2×). To move a leg onto
> a Namespace profile once the org provisions one, set the repo variable
> `DESKTOP_MACOS_RUNNER` / `DESKTOP_WINDOWS_RUNNER` / `DESKTOP_LINUX_RUNNER` to the
> profile name — no workflow edit. Leave `DESKTOP_LINUX_RUNNER` unset unless the
> profile's base image is glibc ≤ 2.35 (Ubuntu 22.04), or the AppImage's portability
> floor rises. Defaults keep the current hosted runners.

## Cutting a release

1. **Actions → Prepare Release → Run workflow.** Choose the **bump**: `patch`
   (default) · `minor` · `major`. Use `dry_run` to preview the version +
   changelog/​pyproject diff without opening a PR.
2. The workflow bumps the version, rolls the changelog, and opens
   `chore: release vX.Y.Z`. It **does not merge or tag** — that's deliberate
   (fleet policy: auto-merge fired on stale SHAs and broke stacked PRs).
3. **Merge the release PR** once the three checks pass (squash).
4. **Push the tag** on the merged release commit — this is what triggers the
   release:
   ```sh
   git checkout main && git pull
   git tag -a vX.Y.Z -m "Release vX.Y.Z" && git push origin vX.Y.Z
   ```
   `release.yml` runs `on: push: tags: 'v*.*.*'` → builds + pushes the semver
   Docker tags, creates the GitHub Release, and posts to Discord.

> **Don't also dispatch the Release workflow by hand after pushing the tag.**
> The tag push already triggers it; a manual `workflow_dispatch` is redundant
> and fails with `422 Release.tag_name already exists` (it leaves a harmless red
> ✗ in Actions — the `[push]`-triggered run is the real one). The dispatch
> trigger exists only to *re-run* a release against a tag that already exists.

Don't bump `pyproject.toml` by hand — Prepare Release owns the version. You do
push the tag by hand (step 4); that tag push is the release trigger.

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
- **The marketing `/changelog`** (`sites/marketing/data/changelog.json`) is
  scaffolded from each release's section by `changelog.py scaffold`. A release
  whose PRs added **no** `[Unreleased]` bullets has an empty section, so it's
  **omitted** from the marketing changelog rather than shown as a bare
  version+date line — add a bullet in your PR for the release to appear.

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
| `GH_PAT` | `prepare-release.yml` | A PAT (not `GITHUB_TOKEN`) so the release-branch push fires the PR's CI checks — the default token can't trigger workflows on its own pushes. (The release tag is pushed by a human, so it triggers `release.yml` normally.) |
| `GATEWAY_API_KEY` | `release.yml` (release-tools) | Rewrites the commit range into themed release notes via the protoLabs gateway. |
| `DISCORD_RELEASE_WEBHOOK` | `release.yml` (release-tools) | Posts the release embed to Discord. **Optional** — the step is `continue-on-error`, so releases still succeed without it; set it to enable the Discord post. |
