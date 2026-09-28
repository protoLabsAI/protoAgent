- **Console: app/theme.css font sizes move onto the DS type scale (#3688).**
  Every hard-coded px `font-size` in `app/theme.css` now reads a bare
  `var(--pl-font-size-{3xs,2xs,xs,sm,base,lg,xl})` token (no px fallback). Five half-pixel
  values snap to the nearest step (theme-invariant): the archetype-preview description and
  playbook description `12.5px → 13px`, the playbook title strong `13.5px → 14px`, and the
  archetype-preview soul block and playbook meta `11.5px → 11px`. Everything else renders
  identically; pre-existing rem/em font-sizes are left untouched. Part of #3688.
