- **Operator API routes no longer leak internals or die on bad input (#3973).** A 500 from
  an `/api` operator route no longer returns the exception text (it leaked file paths and
  library internals): the error is logged with its traceback and the client gets a generic
  message with a short error id to find it by. One malformed bus event no longer ends the
  `/api/events` stream. `/api/runtime/status`, `/api/goals/{id}` and `/api/subagents` are
  guarded like their siblings. A goal spec's `max_iterations` / `no_progress_limit` are
  type- and range-checked on every set path, so `"abc"` is no longer stored and left to
  break the drive loop. With no task store wired, the task routes stop failing with an
  AttributeError 500 and `/api/tasks/status` stops claiming `initialized: true`.
