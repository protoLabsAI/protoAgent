# ADR 0120 — Bound checkpoint growth on the write path, and alert on it

- Status: Proposed
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
- **A latent hazard in the sweep's keep-N.** `task()` subagents inherit the parent's
  checkpointer and write into the parent's `(thread_id, checkpoint_ns)`, tagged with a
  non-empty `metadata.parents`. A newest-N-by-id cap can delete the parent's resumable
  checkpoint while a delegation is in flight.

## Decision

1. **Retention on write.** After every `put`, `ThreadedSqliteSaver` trims that
   `(thread_id, checkpoint_ns)`:
   - It keeps the newest `keep_per_thread` *root* checkpoints (`background_keep` for
     `a2a:background:*`), plus every row newer than the oldest of them.
   - It deletes older rows and their writes.
   - "Root" means `metadata.parents` is empty. Subagent rows never count toward the
     cap and are never cut from in front of their parent.
   - The trim streams newest-first and stops at the cutoff, so it reads only a few
     rows' metadata. That matters because `metadata` sits after the blob in the record.
   - The periodic sweep uses the same `trim_group` rule, on the `(thread, ns)` groups
     that one grouped query finds over their cap.
   - Setting: `checkpoint.prune_on_write` (default `true`).
2. **Media offload.** Before a checkpoint or a pending write is serialized:
   - Strings of at least 32 KiB in message content blocks that are base64 or a
     `data:…;base64,` URI are replaced with `protoagent-ckblob:sha256:<hex>`.
   - The payload is stored **once per thread** in a new `checkpoint_blobs(thread_id,
     hash, data)` table. Payloads are written before the row that references them.
   - Reads (`get_tuple`, `list`) restore the exact strings, so the graph, the model and
     the console see byte-identical messages.
   - A reference whose payload is gone becomes a text block, never a broken image.
   - Prose keys (`text`, `thinking`, `reasoning`) are never offloaded.
   - Blobs follow their thread: `delete_thread` (including the chat-delete cascade), the
     age TTL, and a sweep GC (`gc_blobs`) for payloads that no surviving row references.
   - The GC skips payloads younger than 10 minutes, which covers the gap between a
     payload's commit and its row's.
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
5. **One-time reclaim.** `protoagent checkpoint compact [--workspace <id> | --db
   <path>] [--keep N] [--apply]` is a **dry run by default**:
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
- **Rollback is safe.** Older versions of protoAgent ignore the `checkpoint_blobs`
  table, but would read references verbatim in threads written by this version.
  Downgrading an agent mid-thread shows those images as reference strings until
  the thread ages out.
- **Retention on write costs something per write:** one indexed `SELECT` over a few
  rows, plus one `DELETE` when the thread is over its cap.
