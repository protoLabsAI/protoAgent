- **A repeat scheduled-task chip is no longer a dead end — it expands to the full result (#4726).**
  After a schedule's first fire in a chat (which gets the full ScheduledReportCard), later fires
  collapse to a compact one-line chip so an hourly job can't fill the thread with cards (#2990).
  That one-liner used to be the only affordance — the run's result was reachable only through a
  90-character hover tooltip or the Activity log. The chip now expands in place on click (and
  Enter/Space, with `aria-expanded`) into the same body the first-fire card shows: the full
  summary, the fire time, and a working "View full result". A second toggle collapses it, expand
  state is local per message, and the dismiss ✕ still works in both states without toggling the
  expand. The first-fire card and the recurring chip now share one `ScheduledResultBody`.
