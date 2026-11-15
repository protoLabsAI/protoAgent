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

**Never put a number in the bold lead** (#4874, #5295). You don't know the PR number when
you write the fragment, so any `(#N)` in the lead is a guess — and a guessed number ships
wrong. At release, `collate` reads the PR that actually **merged** the fragment — from the
squash-merge subject's `(#N)`, or a `Merge pull request #N` commit — and stamps `(#<that
PR>)` into the lead for you. The entry above ships as `**… didn't achieve (#2286).**`. So
the fragment is right in the PR's first commit, and you never push a second commit to rename
it or add the number. Before this, a PR with no issue pushed that second commit, and each
push ran another full review.

If a lead does carry a wrong number, `collate` replaces it with the merging PR and prints a
warning — but don't rely on that safety net: the PR gate **rejects any `#N` in the bold
lead** (`leave the number out; collate stamps the merging PR`), so the mistake can't reach
release. The gate also rejects a placeholder like `(#NNNN)` for the same reason.

To point at an **issue**, mention it in the **body text**, outside the bold lead:

```markdown
- **Fleet stop no longer reports a stop it didn't achieve.** Fixes #2286 — the endpoint
  returned `{"ok": true, "stopped": true}` while the process kept running…
```

A ref there is kept verbatim. Only the `**…**` lead survives into the marketing `/changelog`
bullet (`changelog.py scaffold` → `_titles`, #3291), and the PR gate counts entries by it,
so every bullet still needs a bold lead — just without a number in it.

## What happens at release

`scripts/changelog.py collate` stamps the merging PR into each lead (above), folds every
fragment into `## [Unreleased]`, grouped under one heading per kind, then deletes the
fragments. `roll` promotes that section to a dated
version. Both run in `prepare-release.yml`, so `CHANGELOG.md` is only ever edited by the
release process.

`CHANGELOG.md` keeps only `[Unreleased]` + the current month; older releases are moved to
dated `CHANGELOG-YYYY-MM.md` archives (see **Older releases** at the top of `CHANGELOG.md`).
That's a monthly maintenance step (`changelog.py archive`) — it doesn't affect how you add
a fragment. See [`docs/guides/releasing.md`](../docs/guides/releasing.md#monthly-archives-2437).
