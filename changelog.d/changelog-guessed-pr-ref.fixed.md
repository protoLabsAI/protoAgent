- **Release notes link the PR that actually merged a changelog fragment, not a number the
  author guessed.** `collate` now resolves the adding PR for every fragment and replaces a
  bold lead that cites the wrong number (a guessed next-PR, or the source issue) with the
  merging PR, warning about what it dropped; the PR gate rejects any `#N` in a new fragment's
  bold lead so the guess never reaches release. Eight of fifteen v0.242.0 entries had cited
  the wrong PR.
