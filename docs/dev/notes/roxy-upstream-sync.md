# Note: syncing roxy from upstream protoAgent

**Topic:** operational playbook for porting protoAgent work into the roxy fork.
**Status:** current as of 2026-06-02 (roxy caught up through #476 via roxy#27).

## What roxy is

`protoLabsAI/roxy` is a **manual fork** of protoAgent (a ProtoMaker portfolio
manager — monitor + unblock, not code). It's **self-maintained** by its own
agent/fleet: `origin/main` is **protected** (PR-based, no direct push), it has
its own changelog narrative (last release `[0.8.0]`, does NOT carry protoAgent's
version headers), and its own PR series (#1–#27+).

Remotes in `~/dev/roxy`: `origin` = roxy, `upstream` = protoAgent.

## The sync procedure

1. **Fetch origin first — the local clone goes stale.** `~/dev/roxy` lagged
   `origin/main` by ~20 commits in one case; a stale local main produces a
   divergent merge you can't push.
   ```sh
   cd ~/dev/roxy && git fetch origin && git fetch upstream
   ```
2. Check the real delta: `git log --oneline origin/main..upstream/main`.
3. Branch off **origin/main** (not local main) and merge upstream:
   ```sh
   git checkout -b chore/sync-upstream-<x> origin/main
   git merge upstream/main
   ```
4. Resolve conflicts (hotspots below), run tests, open a PR against roxy, merge.

## Conflict hotspots

- **`server.py` — `_SKILL_SPECS` / `_agent_skills()`.** protoAgent's
  `_agent_skills()` is spec-driven off `_SKILL_SPECS`. roxy must **keep its own 5
  PM skills** (`portfolio_sitrep` / `board_sweep` / `project_decompose` /
  `unblock_feature` / `chat`) in `_SKILL_SPECS` while adopting upstream's
  machinery. They're free-text (no `output_schema`) for now — declaring a schema
  later turns on the #476 finalizer with no further wiring.
- **`CHANGELOG.md` — `[Unreleased]`.** roxy keeps its own narrative. Take **only
  the genuinely-new entries** (from `git log origin/main..upstream/main`), merge
  them into roxy's existing `[Unreleased]` sections. **Do not** import
  protoAgent's `## [X.Y.Z]` version headers.

## Running roxy's tests without its own venv

roxy shares deps with protoAgent; the protoAgent venv has the needed packages:
```sh
PYTHONPATH=~/dev/roxy ~/dev/protoAgent/.venv/bin/python -m pytest -q
```

## Known follow-up

roxy still runs the **old `a2a_auth.py`** (pre-#482). It'll inherit the
caller-authoritative-bearer + browser-only-origin fix on its next upstream sync.
Not urgent.
