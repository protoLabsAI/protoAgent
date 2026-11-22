- **The decisions ledger can hold a decision that has no paused turn behind it.**
  A decision record can now be written with a chat thread and no pause id. Such a record
  stays pending until it is answered: the stranded-decision check, which closes a record
  whose paused turn has disappeared, never touches it. Each record also gains three
  fields: `requires_pick` (only a listed option may answer), `reminded_at` and
  `delivered_at`, with `mark_reminded` and `mark_delivered` to stamp them once. An
  existing database gains the columns when it is opened. This is groundwork only:
  nothing reads the new fields yet, and decisions that pause a turn, spend-cap decisions
  and restart-recovery decisions behave as before. Refs #5678.
