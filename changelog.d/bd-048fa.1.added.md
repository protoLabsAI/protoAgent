- **Activity entries carry the inbox item they ran (bd-048fa).** The Feed redesign
  (docs/design/feed.md) joins a queued inbox item to the activity entry it becomes.
  `ActivityLog` now stores an `inbox_id` column (added to existing DBs through the
  additive migration), accepts `inbox_id` on `add()` — an int or numeric string
  round-trips, anything else stores as NULL — and returns it from `recent()`.
