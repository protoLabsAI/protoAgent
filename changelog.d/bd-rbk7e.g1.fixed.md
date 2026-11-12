- **Undelivered priority-now inbox pages retry when the agent is free — not only at the next restart (bd-rbk7e).**
  A now page whose live delivery failed because the agent was mid-turn used to sit
  pending for hours, surfaced only as a dismissable "undelivered page" alert, because
  recovery ran once at boot. Delivery is now retried automatically the moment the last
  in-flight turn settles, plus a bounded sweep every minute that runs only while no
  turn holds the agent — reusing the existing claim/accept safety, so a page is still
  delivered exactly once. While it waits, the page is a calm *queued* row ("Queued —
  runs when the agent is free"), and `/api/inbox` rows carry `queued_since`,
  `attempts` and `retries_exhausted`; only an operator-class page raises a Needs-you
  alert — a declared `needs_human` on `POST /api/inbox`, or a page from a known
  operator-class producer (a projectBoard block page), so existing plugins that cannot
  pass the flag keep paging. Waiting out a busy stretch never spends the bounded retry
  budget; once that budget is spent on genuine failures, the item records a visible
  failed row in its Activity feed with the reason and its feed row stops promising a
  retry — it never pages, and `check_inbox` can still pick the item up.
