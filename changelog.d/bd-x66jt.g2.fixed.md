- **Scheduled result cards stay quiet when a run has nothing new to report (bd-x66jt).**
  A recurring job used to post a ScheduledReportCard to its origin chat on every fire —
  74 cards in two days for one */10 follow-up job — even when the run had nothing to say.
  Now a completed fire posts no card when its answer is empty or is an explicit quiet
  reply (exactly `NO_UPDATE`, or ending with a `NO_UPDATE` line — the `schedule_task` and
  `create_watch` docstrings document the convention), and no card when its normalized
  answer (whitespace collapsed, timestamps masked) repeats the last card delivered for
  the same job generation. The run is still recorded (occurrence, Activity entry); errors
  and failed turns always post, and a failure resets the repeat guard so a recovery run
  posts again. Wait resumes and same-chat suppression are unchanged.
