- **Memory, chat, workflows and the app-crash screen now follow the theme instead of a frozen dark fallback (#3682).**
  These surfaces read CSS variables the pinned design package never defines
  (`--pl-color-text-muted`, `--pl-color-success`/`--pl-color-error`, and a nested
  `--pl-bg`/`--pl-fg`), so the `var()`s never resolved and each site painted its hardcoded
  dark hex — deaf to light mode and to per-agent ThemePanel overrides. They now reference
  the real tokens (`--pl-color-fg-muted`, `--pl-color-status-success`/`-error`,
  `--pl-color-bg`/`--pl-color-fg`) with the same fallbacks, so muted text and the workflow
  status dots/borders track the active theme (e.g. memory muted text resolves to the light
  `#52525b` under `data-theme="light"`).
