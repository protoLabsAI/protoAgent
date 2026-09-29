- **Console: the schedule builder and agent-snapshot CSS move border-radius and off-scale spacing onto DS radius/space tokens (protoContent#525, protoContent#547).**
  `apps/web/src/schedule/schedule.css` and `apps/web/src/settings/snapshot.css` now read the
  `@protolabsai/design` 0.11.0 radius scale (`--pl-radius` / `-md` / `-lg`) for every
  `border-radius`, and the spacing scale — including the protoContent#547 half-steps
  `--pl-space-{0_5,1_5,2_5}` and `-5` — for padding/margin/gap, so both surfaces track the
  operator's chosen density and radii instead of hardcoded px. Off-scale spacing snaps: `3px`→`0_5`,
  `5px`→`1`, `14px`→`3` (schedule mode button) and `14px`→`4` (snapshot source tab). No visual
  behaviour otherwise changes; `1px` hairlines, border widths and element sizes stay as literals.
