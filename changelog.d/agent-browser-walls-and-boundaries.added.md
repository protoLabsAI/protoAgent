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
- **The `fill-form` workflow no longer submits the form.** It fills fields by label,
  reads the form back to verify it, and stops with the filled form for you to review and
  submit. Both bundled browser workflows now load and run: they had an invalid recipe
  shape, and they now run on a new workflow-only `browser` subagent.
