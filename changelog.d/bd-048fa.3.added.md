- **Feed model: inbox items + activity entries merge into one newest-first list (bd-048fa).**
  Feed redesign slice 3 (docs/design/feed.md §1, §3, §4): `apps/web/src/activity/feedModel.ts`
  exports pure functions — `mergeFeed()` turns pending inbox items into quiet "queued" rows
  and activity entries into "ran"/"failed" rows, joined by `inbox_id` so each stimulus
  appears exactly once (the entry wins over its pending item); `applyLiveEntry()` swaps a
  queued row for its result in place (idempotently) or prepends; `groupByDay()` buckets rows
  under Today / Yesterday / short-date headings in local time. The scheduler wake-header
  stripping (#5094) moved into the model (`stripWakeHeader`, re-exported from
  `ActivitySurface`), and the removed Work Activity section's `triggeredByLabel()` is
  re-homed here for the §4 source labels. No UI yet — slice 4 renders it.
