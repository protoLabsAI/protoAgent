- **The stale-fallback cleanup goes tree-wide: no `var(--pl-…, <literal>)` fallback may ship (#3685).**
  Cards 1a–1f dropped every literal `var(--pl-…, <value>)` fallback from `apps/web/src` one surface
  at a time; `app/tokenNameGuard.test.ts` now turns that into a single tree-wide invariant, strictly
  wider than the existing hex sweep. Because the pinned `@protolabsai/design` tokens always load
  before app CSS, any literal fallback is dead weight that can only paint a wrong, theme-deaf value —
  so hex, `rgba()`/`color-mix()`, font stacks, shadows, `inherit` and bare px values are all now
  rejected. The one legal shape is a nested TOKEN fallback whose value is ENTIRELY another
  `var(--pl-…)` read (e.g. `var(--pl-color-fg-subtle, var(--pl-color-fg-muted))`); the fallback is
  parsed with balanced parentheses so an `rgba(…)` or `0 2px 8px rgba(…)` shadow is caught whole.
  Only `app/app-crash.css` (the root error-boundary screen, #872) and `*.test.ts(x)` fixtures are
  exempt. `theme-base.css` is no longer exempt from the hex rule either — card 1a dropped its
  status-alias hex fallbacks — while its `--success`/`--info` status compat aliases stay pinned.
  Both sweeps report sorted `src/<path>:<line>` and self-test their patterns (built by concat).
