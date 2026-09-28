- **Console: drop the app-wide `.pl-dialog__body` padding rule now that dialogs carry the DS `padding` prop (#3688).**
  Every content `<Dialog>` in the console passes `padding="roomy"` (`.pl-dialog__body--roomy`, 24px)
  and the three edge-to-edge dialogs pass `padding="none"` (`.pl-dialog__body--flush`, 0), so the
  body inset is sourced entirely from the design-system `Dialog` component. This removes the redundant
  local CSS: the unscoped `.pl-dialog__body { padding: 24px }` default in `theme.css`, and the
  `padding: 0` counter-overrides on `.settings-overlay`/`.theme-quick-dialog` (`settings.css`) and
  `.goal-create-modal` (`goals.css`). Dialogs render identically — content dialogs keep a 24px body,
  the settings/theme/goal dialogs stay flush with their existing height/overflow/flex behaviour.
  Final part of #3688 (DS 0.63 card 3, Dialog body padding).
