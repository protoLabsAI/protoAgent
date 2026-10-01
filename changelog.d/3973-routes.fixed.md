- **Operator API routes answer honest errors and check their input (#3973).** A 500 from
  an `/api` operator route no longer returns the exception text (it leaked file paths and
  library internals): the error is logged with its traceback and the client gets a generic
  message with a short error id to find it by. A missing scheduler job (`PUT`/`DELETE
  /api/scheduler/jobs/{id}`) or task (`PATCH`, close, `DELETE /api/tasks/issues/{id}`) is
  now a 404 instead of a 400, a 500, or a 200 `{canceled|deleted: false}`. `POST /api/chat`
  maps a failed turn the way `/v1` does (429 mirrored, other upstream failures 502, an
  unavailable model 503, an unknown model 400) instead of a bare 500 or a 200 carrying the
  error. One malformed bus event no longer ends the `/api/events` stream.
  `/api/runtime/status`, `/api/goals/{id}` and `/api/subagents` are guarded like their
  siblings. Input is now checked at the route: a subagent batch takes at most 20 typed
  tasks; `GET /api/background?status=` must be a real job status; every
  `/api/background/{id}` route validates the id; `POST /api/goals` has a typed body (same
  wire shape); a goal's `max_iterations` / `no_progress_limit` must be whole numbers in
  range on every set path; and a malformed schedule or timezone is a 400 before it reaches
  the scheduler. With no task store wired, the task routes answer 503 "tasks not enabled"
  and `/api/tasks/status` reports `initialized: false`.
