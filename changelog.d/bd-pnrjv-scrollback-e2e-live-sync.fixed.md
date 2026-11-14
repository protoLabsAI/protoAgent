- **Web E2E: the `chat-frame-scrollback` 192-block variant no longer times out under shard contention.**
  After live-sync S4 (#5219) the console's sync client refetches the session index and
  re-renders the activity rail on every `/api/events` (re)connect, and the default mock feed
  pushes a frame every 500 ms — all on the page's main thread, which the spec's per-step DPR-2
  paint oracle also drives a multi-million-pixel scan on. Under CI's 4-way shard that competition
  pushed the 192-block scroll loop past its 60 s budget (red on 3 of 12 main runs). The seed-and-
  scroll fixtures now run inside a live-sync scope (as `live-sync.spec.ts` does), so the feed is
  held open but silent and the durable index answers empty — the resync is a clean no-op — and
  Playwright's own trace/video recording is retained on failure only. Both the 48- and 192-block
  variants now stay well under 30 s across 20 consecutive runs.
