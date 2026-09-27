- **Schedule builder, activity feed and code-pane font-sizes move onto the DS type scale (#3688).**
  The three stylesheets carried bare `font-size: <n>px` literals; each now reads the matching
  `--pl-font-size-*` step (10/11/12/13/14px → `3xs`/`2xs`/`xs`/`sm`/`base`) with no px fallback,
  so text sizing tracks the design system. The code-pane's `--diffs-font-size` custom property
  hands `var(--pl-font-size-xs)` (12px) into pierre's diff viewer, so diff code still renders at
  12px. Size-only change — no colour, spacing or layout was touched. Part of #3688.
