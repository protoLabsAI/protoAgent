- **Console: workflows / goals / watches / agent-identity font sizes move onto the DS type scale (#3688).**
  Every hard-coded px `font-size` in `workflows.css`, `goals.css`, `watches.css` and
  `agent/identity.css` now reads a bare `var(--pl-font-size-{3xs,2xs,xs,sm,base,lg,xl})` token
  (no px fallback). Five half-pixel values snap to the nearest step (theme-invariant): the run
  history row and goal timeline reason `12.5px → 13px`, and the run history steps, builder chip
  and goal evidence block `11.5/10.5px → 11px`. Part of #3688.
