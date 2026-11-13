- **A scheduled turn that outlives the fire timeout is "still running", not ERROR "delivery unknown" (bd-snei6).**
  A recurring job whose turn ran past `SCHEDULER_FIRE_TIMEOUT_S` (600s) used to log
  `ERROR … delivery unknown; not automatically retried` with a TimeoutError traceback
  on every long fire — 74 times over a weekend for one */10 follow-up job — even
  though the send was accepted and every occurrence later settled completed. When a
  task handle exists and only the bounded GetTask wait runs out, the occurrence is
  now marked `running`: one INFO line ("still running after Ns; will settle when its
  task ends"), no `last_error`, no traceback, and no failure streak or backoff. The
  ownership hold stays while it runs (the next cron slot waits), reconciliation
  settles it when the task ends, and `turn.finished` plus the result card publish
  once, at real settlement. "delivery unknown" remains for real unknowns: no task id
  after the send, a transport failure before acceptance, or a crash between claim
  and send.
