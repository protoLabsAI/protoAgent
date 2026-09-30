- **Console: the Plugins and folder/file Path-picker surfaces now read the DS radius + spacing scales (#3887, Refs protoContent#525, Refs protoContent#547).**
  Following the workflows/providers card, `settings/plugins.css` and `settings/pathpicker.css` move
  onto `@protolabsai/design` 0.11.0's radius scale (`--pl-radius-md/-lg/-pill`) and spacing
  half-steps (`--pl-space-0_5/-1_5/-2_5`). Every `border-radius` becomes a token — the plugin
  marketplace link / installed-bundle row / path-browser list `8px` and the plugin card `9px` →
  `var(--pl-radius-lg)`, the path-browser row `6px` → `var(--pl-radius-md)`, and the `.plugin-chip`
  `999px` (a genuine pill, not a rectangle) → `var(--pl-radius-pill)`. Off-scale
  `padding`/`margin`/`gap` literals snap to the nearest scale token so the operator's chosen density
  flows through (`2px → --pl-space-0_5`, `6px → --pl-space-1_5`, `10px → --pl-space-2_5`, and the
  snaps `3px gap → --pl-space-1`, `7px → --pl-space-1_5`, `14px → --pl-space-4`); `1px` borders/gap
  and the `18px` deps-list indent stay literal (out of scope). No behavior change.
