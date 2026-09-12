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

### Amendment — trees you own are torn down when the process exits (#3428, 2026-09-10)

A `group_kwargs()` tree leads its own process group, so signalling the owning
process's group never reaches it; its only teardown was the owner's cleanup path.
Two exits pre-empt that path entirely:

- the hub SIGKILLs a member 3s after SIGTERM (`supervisor.shutdown_all`), while the
  member's uvicorn drain alone can take `timeout_graceful_shutdown` (5s), so the
  lifespan teardown that closed its shell / ACP / `execute_code` trees never starts;
- on the desktop, the Tauri shell SIGKILLs the sidecar and the parent-death
  watchdog `os._exit(0)`s — no lifespan, no atexit — in the hub **and in every
  member**, which inherit `PROTOAGENT_PARENT_PID`.

Both left owned trees running at ppid=1. So `infra/proc` also owns a registry:

- **`track_tree(pid)` after spawning a `group_kwargs()` tree, `untrack_tree(pid)`
  once the owner has reaped it** — not before: a cancelled command the owner
  abandoned stays tracked, which is exactly what lets the exit reach it. A pid in
  this process's own group (or this process itself, on Windows) is refused.
- **`begin_tree_teardown()` at exit-signal receipt**, before the drain: SIGTERM
  every tracked tree now, SIGKILL survivors after `TEARDOWN_GRACE` (1.5s — inside
  the hub's 3s, so a SIGTERM-ignoring tree is dead before its member can be
  SIGKILLed). Non-blocking, idempotent, never raises. `server.build_uvicorn_server`
  calls it from `handle_exit`.
- **`reap_tracked_trees()` as the blocking final sweep** — at the end of lifespan
  shutdown (the last chance before the restart route's `os.execv`), in the
  watchdog before `os._exit`, and `atexit`.

Groups are killed by the pgid recorded at track time, not looked up from the root
at teardown: the root is often the first to die (`sh -c` under a long `pnpm
install`), and a group outlives its leader. A recorded pgid is only safe while the
group exists — once it's gone the id can be handed to someone else's group — so
tracking a new tree prunes dead groups, and an owner that stops waiting on a
still-running child (a cancelled turn) forgets it once it's reaped
(`untrack_when_reaped`). **Windows gap:** `taskkill /T` walks from the root, so
descendants that outlive their root are out of reach there — the case that would
justify the Job Object upgrade this ADR already defers. `shutdown_all`'s 3s is unchanged — it
is bounded by the hub's own graceful window, and owned trees no longer depend on
the member's lifespan finishing.

Plugins that spawn their own trees use the same seam (projectBoard's gate/test
children are the first candidate, once this is in a release).

### Amendment — trees whose owner died without teardown are swept (#3463, 2026-09-12)

The #3428 registry lives in memory, so it dies with its owner. An owner that is
**SIGKILLed** (the Tauri shell killing the hub sidecar, an OOM kill, `kill -9`) or
that **crashes** runs no hook at all: not the signal-receipt teardown, not atexit, not
the watchdog. Its trees run on at ppid=1. `codex-acp` doesn't even exit when its
stdin loses its writer. Sixteen of them ran for 36h (~780 MB) before a hand cleanup.
So the registry is also kept on disk, and swept:

- **One record per owning process:** `<box_root>/.owned-trees/<pid>.json`
  (`InstancePaths.owned_trees_dir`, BOX tier beside `.instances/`, so any instance on
  the machine can reach it). It holds the owner's start time and each tree's root,
  pgid and tracking time. `track_tree`/`untrack_tree` rewrite it atomically. The
  exit-signal path never writes it (no file IO between bytecodes); a stale entry naming
  a gone group costs nothing. `reap_tracked_trees` settles it, so a clean exit leaves
  no record.
- **`sweep_orphaned_trees()`** reaps the groups of every record whose owner is gone,
  meaning its pid is dead or now belongs to a process that started later. The server
  sweeps at boot and every `ORPHAN_SWEEP_INTERVAL_S` (10 min), off the loop.
- **Nothing is killed on a guess.** A group is signalled only while it provably is the
  recorded one: it still has members; if its leader (pid == pgid) runs, that leader
  started *before* the record; if it's leaderless (a launcher that died, its binary
  still running), POSIX never reissues a live group's id. The check is repeated before
  the SIGKILL. An owner whose start time can't be read counts as alive: a missed reap
  is recoverable, a wrong kill is not.

POSIX only. Windows has no start-time probe here to tell a recycled pid from ours,
and `taskkill /T` can't reach a rootless tree anyway: the same Job Object trigger as
above.

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
taskkill wait) · #1678/#1679 (`pid_alive`) · ADR 0065 (infra tier layering) ·
#3428 (owned-tree registry — exit-time teardown) · #3463 (owned-tree records — the SIGKILL/crash sweep)
