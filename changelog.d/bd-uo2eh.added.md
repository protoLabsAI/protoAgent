- **Live sync S0: the multi-client sync contract suite (#5196).** ADR 0130's D4 contract is
  now a Playwright suite (`apps/web/e2e/live-sync.spec.ts`) driving two real browser contexts
  against one mock server: a send in one client appearing in the other, a server-started turn
  finishing behind a hidden pane, components rendering after an away-and-return, a
  server-idle session never holding a spinner or a queued steer, and drop/reconnect
  convergence. The mock server grew the scoped world behind it — durable per-scope turn rows,
  a seq'd change feed replayable from `?since=`/`Last-Event-ID`, server-started turns, and a
  restart simulation. The two cases that fail on today's main are marked `test.fail()` with
  the id they wait on (ADR 0130 S1–S3; bd-0ad5v / S2+S4), so the suite is green now and flips
  as the slices land.
