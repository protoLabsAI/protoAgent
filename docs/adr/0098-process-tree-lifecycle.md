# 0098 — One process-tree lifecycle: `infra/proc` anchors and kills every child tree

Status: **Proposed**

## Context

Four subsystems spawn children that spawn children, and each carried its own
POSIX-only copy of the same two moves — anchor the child in its own process
group at spawn, kill by group on stop/timeout:

| Consumer | Spawn | Teardown | State |
| --- | --- | --- | --- |
| Agent shell tool (`tools/shell.py`) | async, own group | group-kill on timeout | **migrated (this ADR's PR)** — was the #2416 bounded Windows repair |
| ACP delegates (`plugins/coding_agent/acp_client.py`) | async, `start_new_session` | `SIGTERM`→`SIGKILL` group escalation + sync atexit kill | to migrate |
| Fleet members (`graph/fleet/supervisor.py`) | sync, detached (survives the CLI) | `SIGTERM`→`SIGKILL` by PID, identity-checked | to migrate |
| `protoagent up/down` (`server/cli.py`) | sync, detached | `SIGTERM`→`SIGKILL` by PID (own `_pid_alive` copy) | to migrate |

None of the POSIX moves exist on Windows (`os.killpg`, `SIGKILL`, `setsid()`),
which is how the Windows release shipped with timed-out commands leaking child
trees — the runtime half of #2412. The per-file exclusion list
(`tests/windows_native_exclusions.txt`) carries a 21-file process-lifecycle
section that this contract burns down.

## Decision

One module — **`infra/proc.py`** (infra tier: importable by tools/, graph/,
plugins, and server/ alike under the lint-imports layering) — owns:

- `group_kwargs()` / `detached_kwargs()` — spawn anchoring for trees you will
  kill vs. trees that must survive you. POSIX `start_new_session=True`;
  Windows `CREATE_NEW_PROCESS_GROUP` (+ `CREATE_NO_WINDOW` for detached).
- `kill_tree(pid, force=)` / `akill_tree(proc)` / `terminate_tree(pid, grace=)`
  — sync, asyncio, and graceful-escalation teardown. POSIX `killpg`; Windows
  `taskkill /T` with every wait **bounded** (a stalled taskkill must never
  extend the caller's own timeout — the #2413 review finding), falling through
  to an immediate kill of the root.
- `pid_alive(pid)` — re-exported from `infra.paths` (#1679): Windows
  `OpenProcess` + `STILL_ACTIVE`, never `os.kill(pid, 0)` (the #1678
  sidecar-suicide class).

**Windows tree primitive: `taskkill /T`, not Job Objects — for now.** Job
Objects are airtight (a tree that cannot detach) but need pywin32 or ctypes
surface we'd have to freeze into the sidecar and maintain; `taskkill` ships on
every supported Windows and walks parent links, which covers the observed
failure mode (orphaned grandchildren on timeout/stop). If tree-escape via
re-parenting shows up in practice, upgrade this module in place — consumers
don't change.

## Consequences

- Direct `os.killpg` / `start_new_session=True` / signal-escalation code
  outside `infra/proc.py` is a smell; new spawns use the kwargs helpers.
  Migration order: shell (done) → ACP client → fleet supervisor + CLI
  (the CLI's private `_pid_alive` copy collapses onto the shared probe).
- `tests/test_proc.py` is the portable acceptance suite (it runs on the
  Windows CI gate); each migrated subsystem rewrites its POSIX-only lifecycle
  tests against it and comes off the exclusion list.
- Fleet stop keeps its PID-identity check (cmdline match before kill) — that
  logic stays in the supervisor; `infra/proc` only owns the mechanics of
  anchoring, probing, and killing trees.

## Refs

#2412 (phase 2) · #2416 (shell repair this extracts) · #2413 review (bounded
taskkill wait) · #1678/#1679 (`pid_alive`) · ADR 0065 (infra tier layering)
