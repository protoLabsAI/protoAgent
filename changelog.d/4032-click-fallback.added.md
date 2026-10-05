- **`browser_click` can fall back to a JS click, and the web-browse skill now teaches form filling (#4032).**
  On Greenhouse the résumé "Enter manually" button opened its textarea only after a
  JS-dispatched click; a plain CLI click exited 0 and nothing happened, and the agent had no
  written doctrine for forms so it fell back to snapshot/`@eN`/type+Enter — the cause of most
  of the failures in the issue. `browser_click` now takes an optional `js_fallback`: when set,
  it fingerprints the page (element count + `document.activeElement` + the target's
  `aria-expanded`), does the normal CLI click, re-fingerprints, and ONLY if nothing moved
  resolves the element in the page and dispatches a bubbling `mousedown`/`mouseup` plus
  `el.click()`, reporting `Clicked <x> (JS fallback)` — never a second click when the first
  already worked. The default (`js_fallback=false`) is byte-for-byte unchanged, and the
  selector also accepts the bd-12mo.1 label locator (`@eN` and CSS still pass through). The
  web-browse skill gains a **Filling forms** section: start with `browser_form_read`, address
  fields by LABEL not `@eN`, use `browser_select` for every dropdown/combobox/phone-country
  picker (never type+Enter), pick the phone country before the number, `browser_upload` a file
  made with `browser_pdf`, reach for `js_fallback` when a click visibly does nothing, read the
  form back and diff every value — and never click submit, or touch a captcha/login, without
  the operator saying so.
