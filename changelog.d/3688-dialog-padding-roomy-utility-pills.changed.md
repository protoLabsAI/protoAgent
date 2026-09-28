- **Console: content dialogs opt into DS `Dialog padding="roomy"` — utility pills (#3688).**
  The shared utility-pill dialog (`UtilityWidget`, covering every consumer) plus the Background
  agents, Work folders and New task dialogs now pass `padding="roomy"`, adding the DS 0.63
  `.pl-dialog__body--roomy` modifier (`var(--pl-space-6)` = 24px). This is a no-op while the
  app-wide `.pl-dialog__body` theme.css rule still forces 24px; it opts these bodies in ahead of
  a later card that deletes that rule, so they keep their padding instead of falling back to the
  DS default `var(--pl-space-4)` = 16px. Part of #3688.
