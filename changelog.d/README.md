# changelog.d — news fragments

One file per PR. **Never edit `CHANGELOG.md` directly in a feature PR** — the CI gate
enforces this: a `CHANGELOG.md` edit does not satisfy it. That is the whole point: every PR used to write to the same three lines under `## [Unreleased]`, so
two PRs in flight conflicted *by construction* and a stack of N cost O(N) serial merges
(#2322).

## Adding an entry

Create `changelog.d/<slug>.<kind>.md` in the **same commit as the change**. The slug only
has to be unique, so use your branch name, or the issue number if there is one:

```
changelog.d/fix-fleet-stop.fixed.md
changelog.d/2119.added.md
```

The slug is letters, digits, `.`, `_` and `-`. `<kind>` is one of: `added` · `changed` ·
`fixed` · `removed` · `deprecated` · `security` · `docs`. Anything else is rejected by the
PR gate and the release collation, loudly, rather than being silently dropped into the
wrong heading.

The file's contents are the markdown bullet(s) exactly as they should appear, each
starting with a **bold lead-in**:

```markdown
- **`POST /api/fleet/{name}/stop` no longer reports a stop it didn't achieve.**
  The endpoint returned `{"ok": true, "stopped": true}` while the process kept running…
```

Write it the way you'd want to read it in release notes six months from now: what broke,
why it mattered, what changed. Same bar as before — only the destination moved.

**You don't need the PR number** (#4874). At release, `collate` puts `(#N)` inside every
bold lead that doesn't already cite a number. It reads the number from the commit that
added the fragment to `main`: the squash-merge subject's `(#N)`, or a `Merge pull request
#N` commit. The entry above ships as `**… didn't achieve (#2286).**`. So the fragment is
right in the PR's first commit, and you don't push a second commit to rename it or add
the number. Before this, a PR with no issue pushed that second commit, and each push ran
another full review.

To cite an **issue** (or several PRs) instead, put it inside the bold lead yourself —
`**Short summary (#2286).**` — and it is left alone. Don't write a placeholder like
`(#NNNN)`; the gate rejects it, because the stamp would land next to it. A ref at the end
of the paragraph, outside the bold, doesn't count: the marketing `/changelog` bullet is
derived from the `**…**` lead ALONE (`changelog.py scaffold` → `_titles`, #3291). That is
why every bullet needs a bold lead (or a `#N` in its first clause); the PR gate checks it.

## What happens at release

`scripts/changelog.py collate` stamps missing PR numbers (above), folds every fragment into
`## [Unreleased]`, grouped under one heading per kind, then deletes the fragments. `roll` promotes that section to a dated
version. Both run in `prepare-release.yml`, so `CHANGELOG.md` is only ever edited by the
release process.

`CHANGELOG.md` keeps only `[Unreleased]` + the current month; older releases are moved to
dated `CHANGELOG-YYYY-MM.md` archives (see **Older releases** at the top of `CHANGELOG.md`).
That's a monthly maintenance step (`changelog.py archive`) — it doesn't affect how you add
a fragment. See [`docs/guides/releasing.md`](../docs/guides/releasing.md#monthly-archives-2437).
