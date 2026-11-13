- **Feed: the old provenance row is gone (bd-048fa).** Feed redesign slice 7, cleanup
  (docs/design/feed.md §2, §9.8): the exported `OriginProvenance` component — the
  pre-Feed origin-chip/trigger/priority/relative-time row that nothing rendered since
  the slice-4 rows and slice-5 detail took over — is deleted from `ActivitySurface.tsx`
  along with its `ORIGIN` icon map, the lucide imports and the `Badge as FailedBadge`
  alias its test guard forced (the failed-row badge is a plain DS `Badge` again), and
  `OriginProvenance.test.tsx` with it. The now-unused `.inbox-pri*` rules leave
  `theme.css`, and the dead `.activity-prov`, `.activity-origin*`, `.activity-trigger`,
  `.activity-time`, `.activity-msg` and `.activity-role` rules leave `activity.css` —
  every deleted class grep-verified to have no remaining user. No behaviour change: the
  Feed list, detail and pill look and act the same.
