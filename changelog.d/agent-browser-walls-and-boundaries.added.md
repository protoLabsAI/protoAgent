- **The browser no longer reports a bot check as a page it opened.** `browser_open` used
  to return `✓ Just a moment...` — a Cloudflare challenge's title — as a successful
  navigation. It now checks the landed page and returns `Error: blocked by <wall> at <url>`
  with "ask the user to clear it in the console Browser panel, then continue" for
  Cloudflare, "checking your browser", DataDome, PerimeterX, Akamai, Google's
  unusual-traffic page, CAPTCHA pages, HTTP 403/429, `ERR_BLOCKED_BY_CLIENT`, and sign-in
  walls. A challenge that clears itself is re-checked for about 5 seconds first. Turn it
  off with `agent_browser.detect_walls: false`.
- **Page text reaches the model marked as untrusted.** Snapshots, page text and HTML,
  and form reads now arrive inside the agent-browser CLI's `AGENT_BROWSER_PAGE_CONTENT`
  markers (a random nonce per call), and the `web-browse` skill tells the agent that text
  inside them is data, never instructions. On by default; `agent_browser.content_boundaries:
  false` turns it off. `browser_eval` output stays raw — it's the result of the caller's own
  script (often JSON a caller parses), not page prose, so it's never wrapped.
- **The bundled browser workflows load, run, and stop at a wall.** `fill-form` and
  `browse-and-extract` had an invalid recipe shape; both now load and run on a new
  workflow-only `browser` subagent, stop and hand a bot check or sign-in wall to you instead
  of pushing on, and `fill-form` still never submits (it fills by label, reads the form back,
  and stops). `browse-and-extract` reads the page text with `browser_get_text` rather than
  the interactive-only snapshot.
