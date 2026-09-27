- **Code viewer, activity feed and schedule builder drop their dark-only token fallbacks (#3685).**
  These three stylesheets read `var(--pl-…, #hex)` custom properties, but the design
  package loads before any app CSS so the `--pl-*` tokens always resolve — the hex
  fallbacks could only ever paint a frozen dark colour, never help. They are removed, and
  the retired `--brand-*` aliases are re-pointed to real DS tokens: the scheduler origin
  tint uses `--pl-color-chart-series2`, the a2a origin uses `--pl-color-chart-series8`, and
  the calendar's selected day uses the `--pl-color-accent` / `--pl-color-fg-on-accent` pair
  instead of a hardcoded violet with white text. All now track light mode and per-agent
  ThemePanel overrides.
