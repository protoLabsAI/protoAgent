- **Browser snapshots are 5-8x smaller, and over-cap output comes back usable.**
  `browser_snapshot` now returns the CLI's interactive, compact view (`snapshot -i -c`) by
  default. It keeps every field, button, link and heading with its `@eN` ref, and leaves body
  text to `browser_get_text`. Measured live: a Wikipedia article went from 381 KB, which was
  over the 200 KB cap and returned nothing usable, to 71 KB. A Greenhouse application went
  from 23.8 KB to 2.8 KB. `full=true` returns the old tree, and new `scope=` and `depth=`
  options narrow it. When `browser_snapshot`, `browser_get_text` or `browser_get_html` output
  passes `max_response_bytes`, the tool now returns the first part, cut on a line break and
  marked `…[truncated: …]` with how to narrow it, instead of a bare "output exceeded" error.
  Reading still stops at the cap.
- **New `browser_wait` tool replaces sleeps and polling loops.** It waits for one of a CSS
  selector, page text, a URL substring, a load state, or a JS condition. The timeout is
  bounded (25 s at most) and always passed to the CLI explicitly. Without one, the daemon's
  30 s default equals the CLI's IPC timeout, so a wait that never matched was re-sent five
  times and blocked the session for about 2.5 minutes. `browser_screenshot` gains
  `full_page=` and `annotate=` (ref labels drawn on the image).
- **Dotted ids and stale refs no longer dead-end the agent.** A `#id` with CSS-special
  characters, such as `#input-candidate.name-3` from `browser_form_read`, used to fail with
  "Element not found". It now resolves to the element with that exact id when it matches
  nothing as CSS. A stale or unknown `@eN` ref now returns an error that says to snapshot
  again or use the field's label, instead of the raw "Unknown ref: e4".
