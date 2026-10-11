- **The Artifact panel stays put when an inline answer arrives, and its frame re-themes fully (#4076).**
  The panel's follow-newest chased the store's current artifact, so an inline svg answered in the same
  chat, or an inline artifact another session made over `/v1`, took over a panel that was showing a
  document. Follow-newest now tracks panel-placed artifacts only (ADR 0118 D2), and a new panel
  artifact still auto-opens. A live theme switch now flips the frame's `color-scheme` along with its
  tokens, so native controls and scrollbars follow it. The shell page's own `color-scheme` follows the
  console theme instead of the OS. An uncaught `send()`/`openLink()` refusal is no longer reported as
  an uncaught promise rejection, and the panel frame drops `allow="pointer-lock"`, which Chrome
  warned is an unrecognized feature. Pointer lock still works through the `allow-pointer-lock`
  sandbox token.
