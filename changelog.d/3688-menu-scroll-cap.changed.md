- **Composer model menu adopts the DS base `.pl-menu` scroll cap and `Menu className` (#3688).**
  `@protolabsai/ui` 0.63 moved the menu scroll cap (`max-height` to the Radix-measured available
  height, `overflow-y: auto`, `overscroll-behavior: contain`) onto base `.pl-menu`, so the console's
  local override in `theme.css` is deleted — every DS Menu now caps and scrolls from the design
  system alone. `<Menu>` also accepts a `className` now (landed on the Radix Content next to
  `pl-menu`), so the composer's model picker carries `className="composer-model-menu"` directly
  instead of a hidden marker child; the phone full-screen-sheet rule retargets from
  `.pl-menu:has(> .composer-model-menu)` to `.pl-menu.composer-model-menu`. Behaviour is unchanged.
