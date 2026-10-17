# ADR 0120 — Bound checkpoint growth on the write path, and alert on it

- Status: Accepted (amended — see Amendment 1)
- Supersedes nothing; tightens the retention contract around the periodic sweep in
  `graph/checkpoint_prune.py` (#3973, #4173).

## Context

`SqliteSaver` stores a checkpoint's **entire** `channel_values` inline, once per graph
super-step. Even a trivial turn writes about 16 rows, one per middleware step. The only
bound was the maintenance sweep (`checkpoint.prune_interval_hours`, 6 h), which keeps
the newest `keep_per_thread` rows per `(thread_id, checkpoint_ns)`.

On 2026-10-16 the brandLaunch fleet member ran one long visual-review A2A turn
(`a2a:reshoot-2k`):

- It wrote about 2,200 checkpoints between two sweeps, at 15–24 GB per hour.
- Each checkpoint was about 55 MB. Of that, about 56 MB was 493 base64 screenshots
  (`image_url` data URIs from `campaign_view` tool results), re-serialized whole into
  every row.
- The file reached 90 GB and the machine's disk reached 99% before an operator noticed.
  No alert existed.

An audit of the 32 local DBs and the remote fleet found:

- **The same payload shape elsewhere**, at smaller scale:
  - cliffNotes: about 31 MB per checkpoint, from `sheet_render` and `pdf_look` crops.
  - bloodBowlCoach: a 1.8 MB pasted PNG carried by every row.
- **A second defect.** `reclaim()` ran `PRAGMA incremental_vacuum` without draining
  its result set, so each sweep freed exactly **one page**. Every `auto_vacuum=INCREMENTAL`
  DB therefore stayed at 90–99% freelist: for example, 3.1 GB holding 6 MB of rows, or
  1.5 GB holding 36 MB.
- **Rows tagged with `metadata.parents` in the thread's own namespace.** brandLaunch's
  `a2a:reshoot-2k` held such rows in namespace `''`, interleaved with the parent's own
  checkpoints. A newest-N-by-id cap can let them push out the parent's resumable
  checkpoint.
  - A `task()` subagent streamed from inside a tool checkpoints into its own
    namespace, `tools:<task_id>` (plus `|1`, `|2`… per `task_batch` child), and
    *every* row there carries `parents`.

## Decision

1. **Retention on write.** After every `put`, `ThreadedSqliteSaver` trims that
   `(thread_id, checkpoint_ns)` and deletes older rows with their writes. The cap is
   `keep_per_thread` (`background_keep` for `a2a:background:*`).
   - **In the thread's own namespace (`''`)** only *root* rows count toward the cap,
     and every row newer than the oldest kept root survives. "Root" means
     `metadata.parents` is empty, so `parents`-tagged rows never push out the parent's
     resumable checkpoint.
   - **In a sub-namespace** (a subagent's own graph), plain newest-N applies, the same
     cap as the old sweep. Root-counting there would never trim, because every row
     carries `parents` (#4306 review).
   - **If the row just written isn't the newest by id**, the trim skips. That happens
     when the clock went backwards across a restart, so LangGraph's time-ordered id
     sorts it below older rows. The trim logs a warning instead of cutting the new turn.
   - The trim streams newest-first and stops at the cutoff, so it reads only a few
     rows' metadata. That matters because `metadata` sits after the blob in the record.
   - The periodic sweep uses the same `trim_group` rule, on the `(thread, ns)` groups
     that one grouped query finds over their cap.
   - Setting: `checkpoint.prune_on_write` (default `true`).
2. **Media offload.** Before a checkpoint or a pending write is serialized:
   - Strings of at least 32 KiB in message content blocks that are base64 or a
     `data:…;base64,` URI are replaced with `protoagent-ckblob:sha256:<hex>`.
   - The payload is stored **once per thread** (Amendment 1: once per DB, `checkpoint_media`) in a new `checkpoint_blobs(thread_id,
     hash, data)` table. Payloads are written before the row that references them.
   - Reads (`get_tuple`, `list`) restore the exact strings, so the graph, the model and
     the console see byte-identical messages.
   - A reference whose payload is gone becomes a text block, never a broken image.
   - Prose keys (`text`, `thinking`, `reasoning`) are never offloaded.
   - Blobs follow their thread: `delete_thread` (including the chat-delete cascade), the
     age TTL, and a sweep GC (`gc_blobs`) for payloads that no surviving row references.
   - The GC skips payloads younger than 10 minutes, which covers the gap between a
     payload's commit and its row's. `compact --apply` uses a 60-second grace.
   - The GC takes the write lock per thread, never across the whole DB.
   - Offload **fails open**: any error stores that checkpoint inline instead of
     failing the write.
   - **Accepted residual:** a turn that re-adds an already-unreferenced, older image
     during a sweep can lose that payload. The block then reads back as the text
     placeholder.
   - Setting: `checkpoint.offload_media` (default `true`).
3. **Reclaim actually reclaims.** `reclaim()` drains `incremental_vacuum` with
   `fetchall()`.
4. **Alerts on the existing banner seam.** `GET /api/runtime/status` `warnings[]` gains
   two live, self-clearing warnings:
   - **This instance:** the DB file plus WAL is over `checkpoint.alert_db_mb` (default
     5120), or one thread is over `checkpoint.alert_thread_mb` (default 1024). The
     warning names the thread and its size.
   - **On a hub:** each local fleet member's DB file is over `alert_db_mb`. This check
     uses `stat` only; the hub never opens a member's live DB.

   The per-thread breakdown is a `length()` aggregate (a header read, not a blob read),
   cached for a minute. `storage.checkpoint_bytes` is unchanged.
5. **One-time reclaim.** `protoagent checkpoint compact` is a **dry run by default**.
   Point it at a DB with `--workspace <id>` or `--db <path>`; `--keep N` sets the cap:
   - Without `--apply`, it lists what the root-aware cap would delete, the bytes freed,
     and the expected file size, using SELECT-only queries that are safe on a live DB.
   - `--apply` trims, GCs blobs and reclaims.

## Consequences

- **Sizes are bounded.** A thread's on-disk size is bounded by `keep × (text state)`
  plus each unique image once. The brandLaunch turn would have held about 2 rows of
  roughly 2.6 MB plus about 56 MB of unique images, instead of 87 GB. Write I/O per
  super-step drops from the full state to the text state plus new images.
- **The model still sees every image.** The model receives exactly what it did before.
  Evicting old images from the *context window* is a separate decision
  (tool-result pruning), outside this ADR.
- **Time travel is limited.** Older checkpoints are gone within a turn rather than
  within six hours. Nothing in core reads history (`get_state_history` has no callers;
  rewind rewrites the latest checkpoint via `aupdate_state`). A fork that needs deeper
  history can raise `keep_per_thread`.
- **Rows are no longer self-contained.** A row that references media can't be decoded
  by a stock `SqliteSaver`. Use `graph.checkpointer.ThreadedSqliteSaver`;
  `scripts/context_audit.py` now does. Copying rows between threads by raw SQL would
  dangle references, which degrade to text blocks. Nothing in core does this.
- **Rolling back is NOT clean for threads that hold images.** An older core ignores
  the `checkpoint_blobs` table, but reads each reference *verbatim*. For example, an
  `image_url.url` of `protoagent-ckblob:sha256:…` would be sent to the model, and the
  provider will likely reject it, failing every turn on that thread.
  - Before downgrading, delete (or let age out) the threads that hold images.
  - Alternatively, run the new core once with `checkpoint.offload_media: false` and
    start a new turn: that rewrites the latest checkpoint inline, but only for images
    still in state.
- **Retention on write costs something per write:** one indexed `SELECT` over a few
  rows, plus one `DELETE` when the thread is over its cap.

## Amendment 1 — offload in the serializer, media per DB

#4306 shipped the offload inside `ThreadedSqliteSaver`, which had a cost: only that
saver could read offloaded rows back. A stock `SqliteSaver`, LangGraph Studio or a
script saw `protoagent-ckblob:` references where the images had been. The offload now
lives in a LangGraph serializer:

```python
from langgraph.checkpoint.sqlite import SqliteSaver
from graph.checkpoint_serde import protoagent_serde

saver = SqliteSaver(conn, serde=protoagent_serde(conn))  # byte-identical messages
```

- **`ProtoagentSerde` subclasses `JsonPlusSerializer`.**
  - `dumps_typed` lifts the payloads out, then serializes the slimmed object.
  - `loads_typed` deserializes, then restores the payloads. Rows without a reference
    skip the lookup: the check is on the raw bytes, with no walk.
  - LangGraph's `with_allowlist` still works: `with_msgpack_allowlist` `copy.copy`s
    the serializer, connection included.
  - `ThreadedSqliteSaver` keeps only retention on write and is built with this serde.
    `checkpoint.offload_media` switches the serde's offload; reads always restore.
- **Media is content-addressed per DB**, in `checkpoint_media(hash, data, created)`.
  A serializer has no thread context, and the same screenshot in two chats is now one
  row.
- **Payload and row commit in one write transaction.**
  - When there are payloads, `store_media` opens `BEGIN IMMEDIATE` *before* checking
    which ones exist, and never commits. The saver's own commit covers the payloads
    and the row together.
  - Holding the write lock from the existence check to that commit makes a dedupe hit
    safe against `gc_media` on another connection. Either the GC ran first, and the
    check re-inserts the payload, or it runs after and sees the row.
- **The connection must be used serially.**
  - `SqliteSaver.put` serializes *before* taking its lock. With media SQL in the
    serializer, that raced other threads' transactions on the shared connection
    ("cannot start a transaction within a transaction"), failing checkpoint writes
    (#4313 review).
  - `ThreadedSqliteSaver` therefore uses a reentrant lock and holds it across the
    whole `put`.
  - A stock `SqliteSaver` with this serde is safe in single-threaded use (scripts,
    Studio), the same constraint LangGraph documents for `SqliteSaver` itself.
- **Liveness is a reference scan.** `gc_media` runs in two phases:
  1. Unlocked, it scans every checkpoint and write row, and takes as candidates the
     payloads older than the 10-minute grace that nothing references. With no
     candidates, which is the steady state, it never takes the write lock.
  2. Under `BEGIN IMMEDIATE`, it rescans *every* row, then deletes the candidates that
     are still unreferenced.
  - A rowid-mark partial rescan missed rows that reused a deleted max rowid. Rows are
    bounded by retention on write, so the full rescan is cheap.
- **Chat delete frees media immediately.** The chat's referenced hashes are collected
  before its rows go, then GC'd at once (grace 0, limited to those hashes). Media
  another chat still references stays. LangGraph's own `delete_thread` deletes rows
  only, and the sweep's GC frees whatever it leaves.
- **Migration.** Opening a DB folds #4306's per-thread `checkpoint_blobs` into
  `checkpoint_media`: one row per hash, keeping the newest `created`. Then it drops
  the old table, idempotently. References in old rows keep resolving, because the
  reference format is unchanged.
  - It runs in one `BEGIN IMMEDIATE` transaction that re-checks for the legacy table,
    so two processes opening the DB at once can't both migrate.
  - A failure is logged and retried on the next open. It never makes the checkpointer
    fall back to in-memory history.
- **Downgrade note:** a #4306-era `main` build opened after this migration recreates
  an empty `checkpoint_blobs`, and reads images as the text placeholder. #4306 is in
  no release tag.
- **Per-thread size alerts** count checkpoint rows (blob + metadata) and pending
  writes. Offloaded media is shared per DB, so it counts in the file-size alert, not
  toward any one thread.
- **The #4306 residual is closed.** A turn that re-uses an already-unreferenced, older
  payload while a GC runs no longer loses it: the existence check and the row commit
  hold the write lock together.
- **Failures don't cascade:**
  - The media GC after a chat delete is best-effort: the rows are already gone, and
    the sweep catches up.
  - Aside cleanup runs it off the event loop.
