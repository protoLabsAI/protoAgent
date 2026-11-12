- **`activity.message` and the activity row carry `inbox_id` (bd-048fa).** Feed redesign
  slice 2 (docs/design/feed.md §3): the A2A executor reads `inbox_id` from the turn's
  request metadata onto `TurnOutcome` (int or numeric string; junk becomes None, never an
  error), the terminal hook passes it to `ActivityLog.add()` and onto the live
  `activity.message` event, and the console's `ActivityEntry` type gains
  `inbox_id?: number | null` — so a feed entry can be joined to the inbox item that
  caused it. A federation caller claiming a server-fired origin loses the key along
  with the other job-claiming metadata.
