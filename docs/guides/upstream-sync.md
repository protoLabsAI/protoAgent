# Sync a fork from upstream

Pull changes from `upstream/main` with a merge commit. Use a real merge rather
than a squash so Git retains the shared history and avoids re-conflicting on
changes already integrated.

## Setup (once per fork)

Two remotes — `origin` (your fork) and `upstream` (this template):

```sh
git remote add upstream https://github.com/protoLabsAI/protoAgent.git
```

And switch the changelog to fork-owned (see [CHANGELOG](#changelog-stop-the-duplicates)):

```sh
# in the fork's .gitattributes
CHANGELOG.md merge=ours
# + per clone / in CI (the driver isn't carried by .gitattributes alone):
git config merge.ours.driver true
```

## The sync

```sh
# 1. Fetch BOTH — the local clone goes stale; a stale local main makes a
#    divergent merge you can't push.
git fetch upstream && git fetch origin

# 2. See the real delta (should be a short list of genuinely-new commits).
git log --oneline origin/main..upstream/main

# 3. Branch off origin/main (NOT local main) and merge upstream.
git checkout -b chore/sync-upstream-$(date +%Y%m%d) origin/main
git merge upstream/main            # review and resolve conflicts (see hotspots)

# 4. Validate this checkout, then open the sync PR.
uv sync --frozen
uv run python scripts/gate.py
gh pr create

# 5. After review and required checks pass, merge as a MERGE COMMIT.
gh pr merge --merge                # NOT --squash
```

Fork **feature** PRs can still squash — they don't touch the upstream base. Only
the *upstream-sync* PR must be a merge commit.

## Why merge, not squash

Squash collapses upstream's N commits into one *new* SHA on your fork. Git never
sees upstream's commit SHAs in your ancestry, so **the merge base never advances**
→ permanent "behind" inflation + recurring re-conflicts on already-integrated
code. A real merge commit makes `upstream/main` an ancestor, so the base tracks
and the next sync shows only genuinely-new commits.

Real example (protoTrader, after two squash-syncs):

| | Squash-synced | Re-done as a merge commit |
|---|---|---|
| Merge base | original fork point (pre-v0.15.0) | real upstream HEAD |
| Behind count | 57 (mostly phantom) | 3 (all genuinely new) |

If your fork is already in the squash-broken state, fix it once: do the next sync
as a true merge commit (reviewing each conflict against your fork's intended changes)
and merge with `--merge`. The base re-anchors and the inflation clears.

## CHANGELOG: stop the duplicates

The template ships `.gitattributes` with `CHANGELOG.md merge=union` — correct for
the template's *internal* feature-branch flow (distinct new entries coexist), but
wrong for an upstream→fork sync: the two changelogs share long history your fork
has curated, so `union` splices upstream's **whole** changelog back in (recurring
duplicate `## [X.Y.Z]` sections to hand-dedupe).

Forks own their changelog narrative — switch it to `merge=ours` (above) so a sync
keeps your changelog and ignores upstream's. Don't import upstream's `## [X.Y.Z]`
version headers; if you want a specific upstream entry, add a fork-owned changelog fragment
using your release process.

## Conflict hotspots

Thanks to the **[operator-fork contract](customize-and-deploy.md#the-operator-fork-contract)**,
a clean fork's conflict surface is now tiny — fork identity & behavior are
config/plugin-driven, so the files you *edit* (and therefore conflict on) should
be almost none:

- **`pyproject.toml` version line** — the one expected trivial conflict; keep your fork's version.
- **`CHANGELOG.md`** — resolved automatically by `merge=ours` (above).
- **Config / persona / plugins** (`config/`, `plugins/`, `SOUL.md`) — fork-owned (`ours`); these are *adds*, not edits, so they rarely conflict.

If you're resolving a conflict in a core `.py` (e.g. `server/a2a.py`, `server/chat.py`),
that usually means you edited a core file instead of using a seam — the card is
config-driven (`a2a.skills`/`a2a.description`), the thread-id is a plugin resolver,
the SSRF allowlist is config. [File the missing seam](https://github.com/protoLabsAI/protoAgent/issues)
rather than re-porting the edit every sync.

## Running the fork's tests

Run the gate from the fork's checkout using its own editable environment:

```sh
uv sync --frozen
uv run python scripts/gate.py
```

The source layout includes packages under `src/` and bundled plugins at the
repository root. Reusing another checkout's virtualenv can import that checkout's
code and give a misleading pass. Follow the fork's `PROTO.md` for additional web,
docs, or platform checks before opening the sync PR.

## Related

- [The operator-fork contract](customize-and-deploy.md#the-operator-fork-contract) — the seams that keep the conflict surface tiny
- [Build an operator fork (Roxy)](operator-fork.md)
- [Customize & deploy](customize-and-deploy.md)
