- **Console: shell / mobile font sizes move onto the DS type scale (#3688).**
  Every hard-coded px `font-size` in `app/theme-base.css`, `app/mobile-shell.css`,
  `app/mobile-native.css` and `app/tools.css` now reads a bare
  `var(--pl-font-size-{3xs,2xs,xs,sm,base,lg,xl})` token (no px fallback). The console body and
  `h1`/`h2` map to `base` (14px, unchanged); the mobile-native `input,textarea,select` iOS
  focus-zoom guard maps to `lg`, which still resolves to exactly 16px. Two values snap to the
  nearest step (theme-invariant): the mobile shell title `15px → 16px` and the tools name
  `12.5px → 13px`. Part of #3688.
