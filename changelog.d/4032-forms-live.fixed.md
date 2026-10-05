- **Browser form tools work against the real agent-browser CLI, now guarded by real-Chrome tests (#4032).**
  The `agent_browser` form drivers — `browser_select`, `browser_upload`, `browser_form_read`
  and `browser_click`'s JS fallback — run their logic as in-page JavaScript and read the
  result back through `agent-browser eval`. The pinned CLI (0.27.1) serialises a returned
  STRING one layer deeper than the CLI-mocking unit tests assumed, so against a real browser
  every one of those tools failed with "the page returned unreadable data" (and a JS fallback
  was mis-reported as a plain click) — a drift the mocked suite could never catch. The forms
  parser now tolerates that extra encoding layer, so the tools actually drive native
  `<select>`, react-select comboboxes, intl-tel-input phone pickers and file inputs in a real
  browser. Saved, self-contained Greenhouse and Ashby application-form fixtures and a live
  test module (skipped unless the CLI and Chrome are present, so the default gate stays
  host-free) lock the behavior in: a vendor markup change now fails a test instead of letting
  the agent submit a wrong answer.
