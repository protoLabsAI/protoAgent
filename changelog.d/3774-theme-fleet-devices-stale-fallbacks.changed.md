- **Theme, fleet and devices stylesheets drop their literal `var(--pl-…, <literal>)` fallbacks (#3774).**
  `@protolabsai/design` loads before any app CSS and `tokenNameGuard.test.ts` already proves
  every `var(--pl-…)` in `apps/web/src` resolves to an installed token, so a literal fallback
  is dead code — and several had drifted from the DS values (the status-alias hexes in
  `theme-base.css`, the `--pl-space-4`/`--pl-radius` px stand-ins, and the mono font stacks).
  `app/theme-base.css` (the `--success`/`--warning`/`--error`/`--danger`/`--info` compat
  aliases, hexes removed but the aliases kept), `app/theme.css`, `fleet/fleet.css` and
  `settings/devices.css` now read the bare `var(--pl-X)`. Value-only edits; nested
  `var(--pl-a, var(--pl-b))` fallbacks and the deliberately-exempt `app-crash.css` are left
  in place. A new `app/dsStaleFallbackDrop.test.ts` pins the four sheets.
