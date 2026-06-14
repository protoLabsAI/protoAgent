---
name: release-notes
description: >-
  Turn a set of merged changes into clean, audience-ready release notes —
  grouped by type (features / fixes / internal), each line written for a
  reader who didn't see the code. Use when asked to draft a changelog entry,
  summarize what shipped, or write release notes for a version.
tools: [run_command, current_time]
user_facing: true
slash: release-notes
---

# Release Notes

Produce release notes a user can read in thirty seconds and know exactly what
changed and whether it affects them.

## 1. Gather the changes
If the input names a range, tag, or PR set, use it. Otherwise inspect the
recent merged history (e.g. `git log --oneline` since the last tag) to find
what shipped. Read enough of each change to describe its *user-visible effect*,
not its implementation.

## 2. Group by what the reader cares about
- **✨ Features** — new capabilities the user can now use.
- **🐛 Fixes** — bugs resolved; lead with the symptom that's now gone.
- **⚙️ Internal** — refactors, deps, CI — fold these into one short line or
  omit unless asked; users don't read changelogs for internals.

## 3. Write each line for the reader
- Start with the user benefit, not the subsystem: "Add a source to the
  knowledge base from a URL" beats "ingestion engine Phase 1".
- One line per change. Link the PR number if you have it.
- Call out anything that needs action (breaking changes, new config, migrations)
  in a **⚠️ Upgrade notes** block at the top.

## 4. Output
A tight markdown block: a one-line summary of the release, the **⚠️ Upgrade
notes** (only if any), then the grouped sections. No filler, no "we're excited".
Scale to the input — a single PR is a couple of lines; a version bump spanning
dozens of merges gets full sections.
