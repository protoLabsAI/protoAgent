- **Undo a task-delete or schedule-cancel from the console (#4955).** The server-side undo
  ledger now covers two more Work view deletes: deleting a task (`DELETE
  /api/tasks/issues/{id}`) and cancelling a scheduled job each stash a snapshot and hand back
  an `undo_token`, and `POST /api/undo/{token}` within the window restores it — a task via the
  new `TaskStore.restore` (re-firing `task.changed` with `action: "restored"`), a job via
  `scheduler.add_job` with its original id, prompt, schedule, timezone, tool fence, paused
  state, timestamps and run history. A one-shot whose
  time has already passed comes back as-is and fires on the scheduler's next tick. Unknown,
  expired or already-used tokens stay a 404, and restoring over an id that was recreated in
  the meantime is refused rather than clobbering the newer record.
