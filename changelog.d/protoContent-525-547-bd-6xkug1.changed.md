- **Console: Settings ▸ Devices and the Telemetry dashboard now read the DS radius and spacing scales (protoContent#525, protoContent#547).**
  Every off-scale `border-radius` and spacing literal in `apps/web/src/settings/devices.css` and
  `settings/telemetry.css` moved onto @protolabsai/design 0.11.0 tokens. The one radius — the QR
  quiet-zone box's `border-radius: 8px` — maps to `--pl-radius-lg` (no 7px/12px radii, no pills).
  Padding/margin/gap map to `--pl-space-0_5…-5`: the lossless half-steps `2 → 0_5`, `6 → 1_5`,
  `10 → 2_5`, the exact steps `16 → -4` and `20 → -5`, and the rhythm snaps `5 → 1_5`, `7 → 1_5`,
  `3 → 0_5`, `14 → -4`. Genuinely off-scale values stay literals (Telemetry's `22px` section
  margin, `18px` insights margin, and the `1px` trace-copy hairline). Visual output is unchanged
  apart from ≤2px snaps on the former 3/5/7/14px sites; the `offScaleSpacing3f` guard's
  devices/telemetry pins were re-pinned to the tokenized strings.
