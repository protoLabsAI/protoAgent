- **Console: edge-to-edge content dialogs opt into DS 0.63 `<Dialog padding="none">` (#3688).**
  The settings overlay (`SettingsOverlay`), the theme quick dialog (`ThemeQuickButton`) and the
  goal-create dialog (`GoalsPanel`) now pass `padding="none"`, which `@protolabsai/ui` 0.63.0
  renders as `.pl-dialog__body--flush` (padding 0). These three dialogs are edge-to-edge today only
  because scoped CSS counter-overrides the app-wide `.pl-dialog__body { padding: 24px }` rule; while
  that global rule still exists the scoped rules win and these dialogs render identically. Declaring
  the intent on the component is what keeps them flush once a later card deletes both the scoped
  `padding: 0` declarations and the global rule (the DS default is 16px). Each dialog keeps its
  scoped className; no CSS changes. Part of #3688 (DS 0.63 card 3, Dialog body padding).
