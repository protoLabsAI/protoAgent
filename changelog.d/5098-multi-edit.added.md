- **`edit_file` can change several places in one file in a single call.** Pass
  `edits=[{"old": …, "new": …}, …]` instead of `old`/`new` and the pairs are applied in order
  to the evolving text, under the same per-path edit lock, CRLF handling and change
  announcement as a single edit. Each `old` must match exactly once at its turn; if any pair is
  missing or ambiguous the call returns `Error: edit N of M: …` and the file is left
  byte-identical. A coding agent no longer pays one model round trip per edit site, or reaches
  for `sed -i` / heredocs through the shell that skip those safeguards. The single `old`/`new`
  form matches and reports as before; an empty `old` or `old == new` is now refused in both
  forms instead of being attempted. Refs #5098.
