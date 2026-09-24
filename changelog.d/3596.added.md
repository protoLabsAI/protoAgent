- **File paths in tool results open in your editor — Zed by default (#NNNN).** `read_file`,
  `search_files` (each `file:line` hit, at that line), `find_files`, `write_file` and
  `edit_file` results now link their paths via `zed://file/…` (or VS Code / Cursor), chosen
  under Settings ▸ Chat ▸ Open files in (per browser; Off restores plain text). Roots come
  from a new read-only `GET /api/fs/roots`, computed from the same fence the fs tools resolve
  against, so a link can't point somewhere the tool didn't read.
