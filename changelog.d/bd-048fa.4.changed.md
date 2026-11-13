- **Feed: one newest-first list — queued, ran and failed rows with day headings; no
  dismiss, deliver or alert (bd-048fa).** Feed redesign slice 4 (docs/design/feed.md §1,
  §2, §4, §7, §8): `ActivitySurface` now renders the slice-3 feed model as ONE list,
  newest first, opening at the top (the oldest-first render and auto-scroll are gone).
  The pending inbox section, its NOW/NEXT/LATER pills, the "undelivered page" alert and
  the dismiss button are removed — nothing in the Feed dismisses, delivers or alerts
  (`api.deliverInbox` is never called; bd-rbk7e retries undelivered priority-now pages
  server-side, and anything operator-class goes to Work › Needs you). Each item is a
  compact composite-button row — `StatusDot` (error when failed), one "Failed" badge
  before a failed title, one-line title + relative time + source/meta line — grouped
  under `h3` day headings in an `ol`, with an `onOpenDetail` hook for the slice-5 detail
  view. A live `activity.message` whose `inbox_id` matches a queued row turns that row
  into its result in place, announced once through a polite live region; `inbox.item`
  refetches so new queued rows appear. Empty / skeleton (`aria-busy`) / one
  couldn't-load `Alert` states per §7.
