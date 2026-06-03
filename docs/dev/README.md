# Internal dev docs

Team-internal engineering notes for protoAgent. **Committed and shared** (so a
teammate or an agent on another machine gets them on `git pull`), but **excluded
from the published docs site** (`srcExclude: ["dev/**"]` in
`docs/.vitepress/config.mts`) — this is working context, not user-facing docs.

## What goes where

| Location | Purpose | Audience |
|---|---|---|
| **`docs/dev/handoffs/`** | Dated session handoffs — "here's where things stand, pick up from here." | Whoever works next (human or agent) |
| **`docs/dev/notes/`** | Engineering logs / investigations — "why is this weird," debugging trails, design scratch that isn't yet an ADR. | Whoever hits the same thing |
| `docs/adr/` | Durable **architecture decisions** (numbered, published). | Forkers + the world |
| `docs/guides/`, `tutorials/`, … | User-facing docs (published). | People using the template |
| `HANDOFF.md` (repo root, **gitignored**) | Live local scratchpad for the *current* session. | Just you, this machine |
| `~/.claude/.../memory/` | The agent's cross-session memory (private, auto-loaded). | The agent |

## Conventions

- **Handoffs:** `docs/dev/handoffs/YYYY-MM-DD-<slug>.md`. At the end of a work
  block, snapshot the live `HANDOFF.md` into a dated file here and commit it.
  Keep `HANDOFF.md` (gitignored) as the always-current working copy; the dated
  files here are the shared archive.
- **Notes:** `docs/dev/notes/<topic>.md`. One topic per file. If a note
  hardens into a decision, promote it to an ADR and leave a pointer.
- **Don't** put secrets, tokens, or live config here — it's committed and ships
  to forks. (Forkers can delete `docs/dev/` wholesale; it's our notes, not
  theirs.)
- This area is **not** part of `npm run docs:build` output — it won't render on
  the site or break the build.
