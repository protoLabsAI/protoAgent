- **Feed: row detail view in the dialog body — "← Feed", dl fields, open-in-reader lives
  here now (bd-048fa).** Feed redesign slice 5 (docs/design/feed.md §5, §8): clicking a
  feed row swaps the list for the new `FeedDetail` in the SAME dialog body — a ghost
  "← Feed" button, a heading that receives focus on open, and a `dl` with what triggered
  it, when (relative time, full stamp on hover) and the full stimulus (wake header
  stripped). A ran entry shows its response as markdown; a failed entry shows "What went
  wrong" with the entry's text; a queued item shows "Status: Queued. It runs when
  <agent> finishes what it's doing now. Nothing for you to do." The open-in-reader
  action (ADR 0062) moves into the detail as a ghost icon button — list rows carry no
  controls. "← Feed" returns to the list and puts focus back on the row that opened the
  detail. Nothing in the detail dismisses, delivers or alerts.
