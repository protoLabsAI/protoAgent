- **Content dialogs opt into DS Dialog `padding="roomy"` (#3688).** The persona version-history,
  auth-required, update-available, and watch-create dialogs now pass `padding="roomy"` so their
  bodies keep 24px padding once the app-wide `.pl-dialog__body` rule is removed by a later DS 0.63
  card. While that global rule still exists these render identically — this is a no-visual-change
  opt-in ahead of the deletion.
