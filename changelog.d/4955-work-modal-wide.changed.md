- **The Work modal opens near-full-screen (#4955).** The large-modal surface host now frames its
  Dialog with the design system's `size="wide"` (DS gap B3, ds-zb8) — a near-full-screen width of
  `calc(100dvw - 2 * var(--pl-space-4))` from `@protolabsai/ui`/`@protolabsai/ui-css` 0.72.0 —
  replacing the DS default width the host carried while the wide size was unavailable, with no
  per-app width override or literal px.
