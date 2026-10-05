- **`browser_upload` attaches a file to a form's file input, fenced to the browser's own capture directory (#4032).**
  Ashby and Greenhouse application forms want a résumé as a FILE, and the `agent_browser`
  plugin had no way to set a file input. The new tool addresses the field by its visible
  LABEL or a CSS selector (re-resolved in the page each call) — and when the located element
  is the "Attach" button or wrapper that hides the real input, it uses the single
  `input[type=file]` in that field's container (none, or more than one, is a clear error).
  `file_path` is fenced exactly like a capture, in reverse: it may only read a file that
  already lives inside the plugin's own capture directory, so a prompt-injected page cannot
  make the agent upload an arbitrary local file (`~/.ssh/...`, a `..` escape or a symlink out
  is refused before the CLI runs, and the file must exist and be non-empty). The résumé flow
  is therefore `browser_pdf` → `browser_upload`, both inside the fence. After the attach it
  reads `input.files[0].name` back and requires it to match the uploaded file — a mismatch or
  empty read-back is a hard `Error:`, and any field-level validation message is surfaced — so
  a silent non-attach is never reported as success.
