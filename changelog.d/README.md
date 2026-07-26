# changelog.d — news fragments

One file per PR. **Never edit `CHANGELOG.md` directly in a feature PR** — that is the
whole point: every PR used to write to the same three lines under `## [Unreleased]`, so
two PRs in flight conflicted *by construction* and a stack of N cost O(N) serial merges
(#2322).

## Adding an entry

Create `changelog.d/<issue-or-pr>.<kind>.md`:

```
changelog.d/2286.fixed.md
changelog.d/2119.added.md
```

`<kind>` is one of: `added` · `changed` · `fixed` · `removed` · `deprecated` · `security`
· `docs`. Anything else is rejected by the release collation, loudly, rather than being
silently dropped into the wrong heading.

The file's contents are the markdown bullet(s) exactly as they should appear:

```markdown
- **`POST /api/fleet/{name}/stop` no longer reports a stop it didn't achieve (#2286).**
  The endpoint returned `{"ok": true, "stopped": true}` while the process kept running…
```

Write it the way you'd want to read it in release notes six months from now: what broke,
why it mattered, what changed. Same bar as before — only the destination moved.

## What happens at release

`scripts/changelog.py collate` folds every fragment into `## [Unreleased]`, grouped under
one heading per kind, then deletes the fragments. `roll` promotes that section to a dated
version. Both run in `prepare-release.yml`, so `CHANGELOG.md` is only ever edited by the
release process.
