- **Console: fleet-room / fleet-activity / app-drawer / work overview font sizes move onto the DS type scale (#3688).**
  Every hard-coded px `font-size` in `app/fleet-room.css`, `app/fleet-activity.css`,
  `app/app-drawer.css` and `app/work.css` now reads a bare
  `var(--pl-font-size-{3xs,2xs,xs,sm,base,lg,xl})` token (no px fallback). Seven half-pixel
  values snap to the nearest step (theme-invariant): the feed source and text plus the
  diagnostics state line `12.5px → 13px`, the roster meta, diagnostics log and pre blocks
  `11.5px → 11px`, and the roster tag `9.5px → 10px`. Part of #3688.
