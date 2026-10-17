# Contributor workflow procedures

[PROTO.md](../../PROTO.md) is the canonical repository policy. This runbook holds
the detailed procedures it links for dependency updates and native test coverage.

## Dependency updates

`gen_attribution.py` reads installed package metadata, so a dependency bump needs
regenerated attribution before its checks pass.

Before updating the branch:

- Review version constraints in `pyproject.toml`. Dependabot can widen a cap even
  when a comment explains why it exists. Put caps that must not move in
  `dependabot.yml`'s `ignore` list.
- Review affected workflows. PR checks do not exercise dispatch/push-only workflows
  such as `desktop-build`, `prepare-release`, `publish`, `release`, `docker-publish`,
  and `marketing-deploy`. Hold action bumps affecting them until after a release,
  then dispatch the affected workflow to verify it.

Regenerate against the PR's locked versions:

```bash
gh pr checkout <pr>
uv sync --frozen
uv run python scripts/gen_attribution.py
git status --short
```

Confirm only `THIRD_PARTY_LICENSES.md` changed, then commit that file and push.
Use `--frozen`: a bare `uv sync` can re-resolve dependencies beyond the scoped bump.

Pushing makes Dependabot stop managing/rebasing the PR. It also changes the actor
seen by the changelog gate, which exempts `dependabot[bot]`. For a dependency bump
with no release-note entry, apply `skip-changelog`. That label re-runs the
changelog gate; manually re-run **Verify workspace config**, whose matching check
does not trigger on label changes.

## Windows checks

The required aggregate is **Windows tests (native)**. Runtime changes run the
platform-sensitive Python suite across two isolated, duration-balanced
`windows-latest` shards:

```bash
python -m pytest tests/ -q -m platform_sensitive
```

The workflow applies [the native exclusions](../../tests/windows_native_exclusions.txt).
That list is a burndown: remove entries, never add them. A nightly run covers the
full Python suite. Tauri-native changes run `cargo test --locked` on Windows.
Known docs/web/marketing-only changes skip the expensive lanes; pushes to `main`
and changes to the classifier/workflow run both.

Tests must carry `@pytest.mark.platform_sensitive` (or module-level `pytestmark`)
when they spawn processes, signal, lock, chmod/symlink, call `os.replace`/`os.rename`,
read `sys.platform`/`os.name`, or import code that branches on the platform.
`tests/test_platform_sensitive_marks.py` enforces this coverage.

After a major suite change, refresh the checked-in duration seed **on Windows**;
a macOS seed gives the wrong shard balance. The monthly workflow also opens a
PR with refreshed timings. To trigger it:

```bash
gh workflow run windows-test-durations.yml
```

Or run on a Windows host:

```bash
uv run --with pytest-split==0.11.0 --with pytest --with pytest-asyncio python -m pytest tests/ -q --store-durations --clean-durations --durations-path tests/windows_test_durations.json
```
