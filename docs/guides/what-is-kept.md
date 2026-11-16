# What is kept, and where

To compare your phone and desktop, connect both to the **same agent instance**.
The server holds its chats and work records; each browser also keeps local
settings and caches. Use the table below to check what ages out, and
[manage memory](/guides/manage-memory#delete-a-chat) to delete a chat or its
saved memories.

## How long records stay

These are the shipped defaults. Retention sweeps remove eligible records on a
maintenance pass, not at the exact moment a time limit expires.

| Record | Retention |
| --- | --- |
| Your chats and their saved turns | Kept until you delete the chat. Closing a tab preserves its history. |
| Outside conversation turns (**From others**) | 7 days, measured from each turn record's last update. |
| Job, background, palette, and Activity session turns | 24 hours from each turn record's last update. These are execution transcripts, not the Work tasks or Activity feed entries below. |
| Work tasks | No automatic age-based pruning, including closed tasks. Closing a task updates its status; explicit deletion removes it. |
| Inbox items | `inbox_retention_days`, configured as `inbox.retention_days`: 90 days by default. Only delivered items are pruned, using their creation date; undelivered items stay. Set `0` to keep all. |
| Activity feed entries | `activity.retention_days`: 90 days by default; `0` keeps all. |
| Background job records | `background.retention_days`: 90 days by default, only for finished jobs whose results were delivered; `0` keeps all. |

The turn sweep preserves records paused for input or authentication, and records
reserved for restart recovery. Those exceptions can outlive the usual 7-day or
24-hour window.

## Checkpoints and saved history are separate

Checkpoints hold the agent's resumable working context. They are separate from
the saved turns you read in a chat. By default, `checkpoint.keep_per_thread`
keeps the latest **2** checkpoints per session; background threads keep **1**
(`checkpoint.background_keep`). Pruning also runs on each write by default.
The periodic sweep runs every **6 hours** and retires checkpoint threads idle
for **30 days** (`checkpoint.prune_interval_hours` and `checkpoint.max_age_days`).
Set either interval or age to `0` to disable that particular limit. Job execution
threads with unresolved decisions or scheduled recovery/continuation work are
protected from age-based retirement.

With `checkpoint.harvest_enabled` on (the default) and the memory stores
available, retirement first attempts to save a summary. This does not make raw
checkpoints permanent. See [conversation history](/explanation/tuning-and-cost#conversation-history)
for the checkpoint controls.

Saved history has its own policy. **`history.retention_dry_run` defaults to
`true`**: the sweep logs what it would remove without deleting history rows.
If you set it to `false`, these age limits apply:

| Saved history class | Default limit |
| --- | --- |
| Reports (`history.report_retention_days`) | 90 days |
| System-session history (`history.system_retention_days`) | 30 days, including those sessions' summaries and archives |
| Other summaries (`history.summary_retention_days`) | `0`: keep indefinitely |
| Other archives (`history.archive_retention_days`) | `0`: keep indefinitely |

These ages use the history row's creation date. A `0` limit keeps that class
indefinitely. The dry-run setting applies to this history-table sweep, not to
checkpoint pruning or explicit deletion.

The separate session-summary files used for memory recall have their own bounds:
`memory.session_retention_days` defaults to **90 days** since last write, and
`memory.session_max_files` to the newest **500 files**. Set a bound to `0` to
disable it.

## What syncs between devices

When both devices connect to the same instance, the server supplies:

- The chat list, titles, and open/closed state.
- Saved turns and the current session state.
- A durable change feed so clients can catch up and refetch changed records.

The change feed keeps **7 days or 100,000 entries**, whichever limit removes an
entry first. A device that falls behind that window refetches current state;
this window does not shorten chat retention. See
[ADR 0130](/adr/0130-server-authoritative-live-sync) for the sync design.

The **From others** hide setting is per device. Hiding it on your phone does
not hide it on your desktop or delete its conversations. Browser storage
(`localStorage`, `sessionStorage`, and IndexedDB caches) is also local to that
browser/origin; it is not a shared server backup. The console rebuilds shared
chat data from the server rather than copying browser storage between devices.

## What deleting a chat removes

Deleting a chat removes its stored turn records and server-held list entry.
The server keeps a deletion marker (a *tombstone*) so a late turn save cannot
bring the deleted chat back into discovery. Other connected devices remove it;
a device that was offline catches up when it reconnects.

**Work tasks associated with the chat remain.** The chat-delete handler removes
A2A turn records, not the separate task tracker entries. Delete those tasks
separately if you want them removed.

The dialog's **Memory** choice controls whether to save a summary, leave earlier
knowledge-store entries alone, or erase conversation-derived memory. All choices
remove the separate session-summary file used for automatic recall. Follow
[Delete a chat and its saved memory](/guides/manage-memory#delete-a-chat) for the
choices and their limits. `/clear` removes the current history while keeping
the chat's tab and reusable session ID; it does not permanently retire the chat.
