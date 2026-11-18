- **The lead prompt tells agents with file tools to batch independent reads, searches and edits in one step.**
  The Guidelines section gains one line — "Make independent reads, searches and edits in the same
  step, not one per turn" — and points at `edit_file`'s `edits` list and `search_files`' `queries`
  list, so a multi-file change costs a few model round trips instead of one per call. The line is
  present only when `read_file`, `search_files` or `edit_file` is bound, and names each list argument
  only when its tool is bound; an agent with no file tools gets the same prompt as before. The
  Guidelines budget ceiling rises from 900 to 1,150 characters and the lead doctrine ceiling from
  4,200 to 4,500 (see `docs/explanation/prompt-contracts.md`). Refs #5098.
