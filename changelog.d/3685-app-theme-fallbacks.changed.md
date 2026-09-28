- **App shell, tools rail and the protoLabs icon drop their dark-only token fallbacks and legacy `--brand-*` aliases (#3685).**
  `app/theme.css`, `app/tools.css` and `app/ProtoLabsIcon.tsx` pinned `var(--pl-…, #hex)`
  fallbacks (activity/field/inbox status tints, input inset, resize handle, tools-row pulse,
  knowledge drop, the accent icon and its gradient) and leaned on the retired `--brand-violet*`
  / `--brand-indigo-bright` aliases (metric/status/setup icons, setup progress + icon chrome,
  settings/setup help links). Since `@protolabsai/design` loads before any app CSS the `--pl-*`
  tokens always resolve, so the fallbacks could only ever paint a frozen dark colour — deaf to
  light mode and to ThemePanel overrides. All hex fallbacks are dropped for the bare
  `var(--pl-…)`; alias TEXT/icon sites now read `--pl-color-accent-fg` (AA on light and dark)
  and fill/border sites read `--pl-color-accent`.
