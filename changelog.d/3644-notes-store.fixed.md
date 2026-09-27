- **Notes plugin resolves its note through the instance store, honouring the box root (#3644).**
  `_note_path()` derived the note's directory straight from the home dir, so an isolated
  server wrote its note into the real home, fleet members sharing a HOME shared one note,
  and a default install stored it one level above the instance root. It now resolves
  through `sdk.plugin_store(plugin_id="notes")` (ADR 0004 / 0065) — the same fix as the
  artifact plugin — and adopts a pre-scoping `~/.protoagent/notes` note (and its history)
  on first access. `NOTES_DIR` is unchanged, and a path-resolution failure falls back to
  the legacy path so a note tool never fails over where its file lives.
