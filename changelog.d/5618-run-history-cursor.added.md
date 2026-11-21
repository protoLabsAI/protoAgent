- **Scheduler run history can be paged with a cursor.** `GET /api/scheduler/jobs/{id}/runs`
  now returns `next_cursor` beside `runs`: an opaque string when the page came back full,
  `null` otherwise. Send it back as `?before=` to get the next older runs, so a client can
  read past the 200-run limit. An unreadable cursor is a 400. The console still uses
  `limit` for "Show older runs". Refs #5543.
