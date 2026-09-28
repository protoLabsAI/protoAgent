- **Content dialogs opt into DS `Dialog padding="roomy"` (#3688).** The path picker, the two
  provider-connection dialogs (add/edit and remove), the QuickSetting editor and the archetype
  "What's included" preview now pass `padding="roomy"` to the design-system `Dialog` (DS 0.63).
  While `theme.css`'s app-wide `.pl-dialog__body` padding still exists this renders identically;
  it makes each content dialog keep its 24px body padding once that global rule is removed.
