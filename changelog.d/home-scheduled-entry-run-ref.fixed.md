- **A scheduled result's Home entry now records which run produced it.** The entry's
  metadata gains the run's `activity_context` and `task_id` next to `kind`, `importance`,
  `source` and `notice_id`, and `/api/home/entries` returns them as-is. This is the
  server half of opening a job's whole answer from its Home card; the console button
  follows. Every other notice's Home entry is unchanged. Refs bd-0p4d8.
