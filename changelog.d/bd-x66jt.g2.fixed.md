- **Scheduled result cards stay quiet when a run has nothing new to report (bd-x66jt).**
  A recurring job used to post a ScheduledReportCard to its origin chat on every fire —
  74 cards in two days for one */10 follow-up job — even when the run had nothing to say.
  Now a completed fire posts no card when its answer is empty or is an explicit quiet
  reply (exactly `NO_UPDATE`, or ending with a `NO_UPDATE` line — the `schedule_task`
  docstring documents the convention; `create_watch` warns it does NOT apply to watch
  reactions, which reply as visible turns in their own chat), and no card when its
  normalized answer (whitespace collapsed, timestamps masked) repeats the last card
  delivered for the same job generation. The recurring-collapse flag now counts
  delivered cards rather than fires, so quiet early runs no longer shrink a job's first
  visible card to a chip. The run is still recorded (occurrence, Activity entry); errors
  and failed turns always post, and a failure resets the repeat guard so a recovery run
  posts again. Wait resumes and same-chat suppression are unchanged.
