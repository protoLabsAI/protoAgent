- **Frame-rendered plugin components theme via the DS embed layer (#4170).** `FrameComponentHost`
  now builds the theme it posts into the opaque-origin component frame from
  `@protolabsai/design/embed` — `themeTokenSnapshot(document.documentElement)` (the live computed
  `--pl-*` values plus `color-scheme`) routed through `snapshotToCss`, the frame-injection guard
  that drops any value containing `<`, `{`, `}` or `;` and any non-`--pl-` name — instead of the
  hand-written `consoleTheme()` token list. The theme is read at post time, so a live theme or
  accent switch still re-posts fresh values and the frame repaints without a reload, and no bearer
  is ever posted. The ADR 0026 plugin-view bridge (PluginView, App, Launcher, the command palette)
  keeps using `consoleTheme()` unchanged. (ADR 0118 D5)
