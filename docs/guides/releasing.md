# Releasing

Prepare a version/changelog PR, merge it through review, then tag its **merged
commit**. Releases are manual; ordinary merges publish a rolling Docker image
but do not create a versioned release.

Prerequisites: `RELEASE_ENABLED=true`, the secrets below, and permission to merge
and push tags. Forks also need their own image name and package publication
configuration; see [Deploy via GHCR](/guides/deploy).

## The flow at a glance

| Action | Result |
| --- | --- |
| Dispatch `prepare-release.yml` | Version, lockfile, and changelog PR |
| Merge that PR and push `vX.Y.Z` at its merge commit | Independent `release.yml` and `publish.yml` runs: semver Docker images, GitHub release, PyPI wheel |
| Dispatch `desktop-build.yml` with that tag | Desktop artifacts; signed updater bundles enable `latest.json` and promotion to Latest |

### Desktop

Desktop builds run on manual dispatch, not tag pushes:

```sh
gh workflow run desktop-build.yml -f tag=vX.Y.Z
```

Wait for the GitHub release to exist before dispatching. The build attaches
macOS, Linux, and Windows artifacts to it. macOS requires signing/notarization
credentials; updater bundles require the updater signing key. Without a tag,
the workflow builds CI artifacts only. See
[desktop build and update configuration](https://github.com/protoLabsAI/protoAgent/blob/main/apps/desktop/README.md#platforms--ci)
for platform requirements and runner overrides.

**Latest means the last promoted desktop release, not the newest tag.**
`release.yml` creates a non-Latest release. The desktop fan-in promotes it only
after uploading `latest.json`, which the in-app updater needs.

If one platform fails, the default fan-in withholds the manifest. Fix and rerun
when possible. For an intentional partial release, dispatch with
`-f tag=vX.Y.Z -f allow_partial=true`. The manifest then covers available signed
bundles; users on missing platforms stay on their previous version. The public
download page is not refreshed for a partial release.

## Cutting a release

1. Dispatch **Prepare Release** with `patch`, `minor`, or `major`. Add
   `-f dry_run=true` to preview instead of opening a PR:

   ```sh
   gh workflow run prepare-release.yml -f bump=patch
   ```

2. Review the resulting `prepare-release/vX.Y.Z` PR, including the version,
   lockfile, collated changelog, and marketing entry. Merge once its checks and
   current-head review are complete. The workflow does not merge or tag for you.
3. Tag that PR's **merged commit**, even if more work has since reached `main`.
   From a checkout of this repository, replace the example PR number and tag:

   ```sh
   RELEASE_PR=1234
   RELEASE_TAG=vX.Y.Z
   RELEASE_COMMIT=$(gh pr view "$RELEASE_PR" --json mergeCommit -q .mergeCommit.oid)
   test -n "$RELEASE_COMMIT" && test "$RELEASE_COMMIT" != null || exit 1
   git fetch origin main --tags
   git show "$RELEASE_COMMIT:pyproject.toml"
   # Confirm its version matches RELEASE_TAG before tagging:
   git tag -a "$RELEASE_TAG" "$RELEASE_COMMIT" -m "Release $RELEASE_TAG"
   git push origin "$RELEASE_TAG"
   ```

4. Wait for **release** and **publish** to succeed. These tag-triggered workflows
   run independently; success in one does not establish success in the other.
5. Dispatch the [desktop build](#desktop) if this release should reach desktop
   users, then verify its artifacts and manifest.

Check each publication channel using the version you released:

```sh
gh release view vX.Y.Z --json tagName,assets,url
docker manifest inspect ghcr.io/protolabsai/protoagent:X.Y.Z
curl -fsS https://pypi.org/pypi/protolabs-agent/X.Y.Z/json | jq -r .info.version
# The currently promoted desktop release:
gh api repos/protoLabsAI/protoAgent/releases/latest --jq .tag_name
```

Use your fork's repository, image, and PyPI package where applicable. Do not
manually dispatch `release.yml` as well as pushing the tag: both runs try to
create the same release. If publication fails, inspect the failed step and
existing artifacts before retrying. Do not move a published tag to another
commit. Prepare Release owns version bumps; feature PRs should not bump them.

## The changelog protocol

Each feature PR adds `changelog.d/<issue-or-pr>.<kind>.md`, with a bold lead-in
including its issue/PR reference. Follow the
[fragment format](https://github.com/protoLabsAI/protoAgent/blob/main/changelog.d/README.md);
do not edit `CHANGELOG.md` directly in feature PRs.

Prepare Release runs `changelog.py collate`, `roll`, and `scaffold`: it consumes
the fragments, creates the dated release section, and drafts the marketing
changelog entry. Those changes are reviewed in the release PR. Empty release
sections are omitted from the marketing changelog.

The `Changelog entry` check requires a fragment except for `release/*` or
`prepare-release/*` branches, Dependabot, or PRs deliberately labeled
`skip-changelog`.

### Monthly archives (#2437)

Keep the current month's releases in `CHANGELOG.md` and older releases in dated
archives linked under **Older releases**. At the first release of a new month,
archive the completed month in the release PR. For example:

```sh
uv run python scripts/changelog.py archive --before 2026-10-01 --out CHANGELOG-2026-09.md
```

Commit both the archive and updated root file. The command moves older sections
verbatim and updates the archive index; `changelog.py notes <version>` also reads
the archives. Feature contributors continue adding fragments normally.

## Branch protection

`main` uses a repository ruleset, not legacy branch protection. Inspect the
current requirements rather than relying on a copied check list:

```sh
gh api repos/:owner/:repo/rulesets --jq '.[] | {id, name, enforcement}'
# Substitute the main ruleset ID printed above:
gh api repos/:owner/:repo/rulesets/RULESET_ID --jq .rules
```

The ruleset requires a PR and status checks, including **Windows tests (native)**
and **Review at head**. Zero required human approvals does not waive current-head
panel review. Inspect its findings as well as the status: a WARN can satisfy the
head check. Follow the
[repository landing requirements](https://github.com/protoLabsAI/protoAgent/blob/main/PROTO.md#landing-a-pr).

## Required secrets

| Configuration | Purpose |
| --- | --- |
| `GH_PAT` secret | Prepare Release checks out/pushes its branch and creates the PR using this token. Grant repository contents and pull-request write permissions so the PR can run downstream CI. |
| PyPI trusted publisher | Configure the target package for this repository's `publish.yml` and `pypi` environment; publication uses OIDC, not an API-key secret. |
| Desktop signing secrets | Required for signed desktop publication; see the [desktop README](https://github.com/protoLabsAI/protoAgent/blob/main/apps/desktop/README.md). |
| `GATEWAY_API_KEY`, `DISCORD_RELEASE_WEBHOOK` secrets | Optional themed release notes and Discord notification. That step is best-effort; Docker/GitHub publication does not depend on it. |
