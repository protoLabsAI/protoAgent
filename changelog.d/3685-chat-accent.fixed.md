- **Chat accents now follow the workspace/theme accent instead of a frozen dark fallback (#3685).**
  The HITL request card (border, wizard dot, option hover/focus/selected fill, checkmark),
  the slash-command user bubble and badge, the success system note and the slash-menu name
  all pinned legacy `--brand-*` aliases or `var(--pl-…, #hex)` fallbacks. Since the DS tokens
  are always loaded, those fallbacks could only ever paint a wrong, dark-only colour — deaf
  to light mode and to per-agent ThemePanel overrides. These sites now read the semantic
  `--pl-color-accent` / `--pl-color-accent-fg` DS tokens directly.
