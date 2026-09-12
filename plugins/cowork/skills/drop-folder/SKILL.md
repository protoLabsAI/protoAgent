---
name: drop-folder
description: Turn a fenced work folder into a drop zone — a watch notices when files land, change, or disappear there and runs a follow-up you define (summarize the new PDF, file the invoice, merge the CSV). Use when the operator wants something to happen "whenever a file shows up in <folder>", or wants to pause or remove such a watch.
---

# Drop-folder workflows

A schedule runs on the clock; a drop zone runs on arrival. protoAgent's watch
primitive polls a condition out-of-band and runs a follow-up prompt in this
session when it moves. The tools: `create_watch`, `list_watches`,
`clear_watch`; the folder check is `cowork:folder_changed`.

## Setting one up

1. **The folder must be fenced.** The check only reads inside the operator's
   fenced work folders (Settings ▸ Tools) — outside them it refuses on every
   poll. If the folder isn't fenced yet, send the operator to Settings first;
   don't create a watch that can never fire.
2. **Distill the follow-up with the schedule skill's discipline.** The
   `run_prompt` runs cold: name the folder by absolute path, say what to do
   with what's new, where results go, and what *done* looks like. The prompt
   fires on *any* movement — arrivals, edits, deletions — so tell it to
   compare against what it already handled and do nothing when nothing needs
   doing.
3. **Create the watch:** `check` = `cowork:folder_changed`, `check_args` =
   `{"path": "/abs/folder", "glob": "*.pdf"}` (add `"recursive": true` for
   subfolders), `on_change: true` so it keeps firing per change rather than
   tripping once. Folders rarely need a fast poll — an `interval_s` of a few
   minutes is plenty — and give `expires_in_s` when the drop zone has a
   natural end (a collection window, a one-week intake).
4. **Read it back**: folder, file pattern, what happens on a drop, and how
   long it lives — so a mis-aimed watch gets caught now, not at the hundredth
   poll.

## Stopping or changing one

`list_watches` to find it, `clear_watch` to remove it; recreate rather than
patch when the folder or follow-up changes. If the operator asks to "pause
the drop zone", clearing and noting how to recreate it is the honest version
— watches don't pause.
