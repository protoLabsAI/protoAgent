- **Browser tools address form fields by label and read whole forms back (#4032).**
  `browser_snapshot`'s `@eN` refs are resolved once and go stale the instant a page
  re-renders — a reload, a React state change, a validation error redrawing a field — which
  is why filling one long Greenhouse form could burn ~50 tool rounds on "Unknown ref". The
  `agent_browser` plugin now lets a tool address a field by its visible **label** (falling
  back to `aria-label`/`aria-labelledby`/`placeholder`/`name`), re-resolved in the page on
  every call, so addressing survives the re-renders that invalidate a snapshot ref; a `@eN`
  ref or a CSS selector still works unchanged. An ambiguous or missing label is a clear
  error (the closest labels, or the fields it matched) rather than a silent action on the
  wrong element. The new **`browser_form_read`** tool returns every field on the page (or
  within a `scope` container) as JSON — `label`, `kind`, `name`, `id`, `required`, the
  committed `value` (a react-select combobox reports its rendered selection, not half-typed
  search text) and `options` — the reliable way to see a form before filling it and to
  verify it after.
