- **Pause/resume a schedule from the console, plus live schedule updates (#4955).** Adds
  `POST /api/scheduler/jobs/{id}/enabled` with body `{"enabled": bool}`: `false` pauses a job,
  `true` resumes it, and the route returns the job with its new `enabled` value. An unknown id
  is a 404; a scheduler backend without `set_enabled` is a 501. The console handlers now also
  publish a best-effort `scheduler.changed` event `{"job_id", "action"}` on the event bus after
  each add, edit, cancel, pause, resume and undo-restore (`action` is `"added"`, `"updated"`,
  `"canceled"`, `"enabled"`, `"disabled"` or `"restored"`), so the Work view's Schedules
  section can refresh live instead of polling. A publish failure is logged and never fails the
  request.
