- **Console: the mobile shell, delegates form and watches panel now read the DS half-step spacing scale (Refs protoContent#525, Refs protoContent#547).**
  Step 3 of the DS-audit spacing migration: every off-scale spacing px on a
  `padding*`/`margin*`/`gap`/`top`/`right`/`bottom`/`left` declaration in `app/mobile-shell.css`,
  `settings/delegates.css` and `watches/watches.css` moves onto the half-step tokens
  @protolabsai/design 0.11.0 shipped — `6px→--pl-space-1_5`, `10px→--pl-space-2_5` (the watches
  row padding, the delegates env-editor gaps, the delegates advanced-group gap/padding-top, the
  watches clear-button `right`), and the exact-scale `8px` sites (mobile toast side gutters, watches
  clear-button `top`) to `--pl-space-2`. The one gap with no exact half-step, the session-row
  `gap: 9px`, snaps down to `--pl-space-2` (8px). The uncovered `34px` watches reveal gutter, the
  `1px` border widths, and every value that reads `env(safe-area-inset*)` — including the pinned
  home-indicator gutter `max(env(safe-area-inset-bottom), 12px)` and the toast `top` offset — are
  left verbatim. Pure token substitution; the only visible change is the ≤1px `gap: 9px→8px` snap.
  The `offScaleSpacing3h` guard's watches/mobile-shell/delegates pins are re-pinned to the
  tokenized strings.
