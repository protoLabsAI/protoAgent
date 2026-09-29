- **Console: Fleet Room's roster and activity feed now read the DS radius and spacing scales (protoContent#525, protoContent#547).**
  Every `border-radius` and off-scale spacing literal in `fleet-room.css` and `fleet-activity.css`
  moved onto @protolabsai/design 0.11.0 tokens. Radii map to `--pl-radius`/`-md`/`-lg`/`-pill`
  (2–5px → base, 7px → `-md` on the small chips / `-lg` on the content boxes, 8–10px → `-lg`, the
  two status/task pills → `-pill`); the `50%` presence dots are left alone. Padding/margin/gap plus
  the mention-popover inset and offsets map to `--pl-space-0_5…-5` (2 → `0_5`, 6 → `1_5`, 10 → `2_5`,
  20 → `-5`, and the 14px header gutters snap up to `-4`). Genuinely off-scale values (1/11/28px,
  border widths, font and element sizes) stay literals. Visual output is unchanged apart from ≤2px
  snaps on the former 5/7/9/14px sites; the `offScaleSpacing3d` guard's fleet pins were re-pinned to
  the tokenized strings.
