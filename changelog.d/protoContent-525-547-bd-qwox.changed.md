- **Console: the Tools panel and Goals side-panel CSS read the DS radius and spacing half-step tokens (protoContent#525, protoContent#547).**
  `apps/web/src/app/tools.css` and `apps/web/src/goals/goals.css` move every hardcoded
  `border-radius` px onto the `@protolabsai/design` 0.11.0 radius scale (`--pl-radius-md`,
  `--pl-radius-lg`) and every off-scale spacing px on padding/margin/gap/top/right onto the scale —
  including the protoContent#547 half-steps `--pl-space-{0_5,1_5,2_5}` — so both surfaces track the
  operator's chosen density and radii instead of frozen literals. Direct half-steps are exact
  (`2px`→`0_5`, `6px`→`1_5`, `10px`→`2_5`, `8px`→`2`); one value snaps by +1px (the tools-row
  vertical `padding: 7px`→`--pl-space-2`, keeping the panel's dominant 8px rhythm). No radius or
  spacing site keeps a raw px; uncovered values stay literal (the goals row's `34px` horizontal
  padding, the goals list's `18px` indent, `1px` hairlines/margins, border/outline widths and
  element sizes).
