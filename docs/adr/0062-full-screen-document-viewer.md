# ADR 0062 — Full-screen document viewer surface

**Status:** Accepted (shipped)

## Context

Background-agent reports (ADR 0050) arrive in the chat as a display-only system card, but the
server carries only a **preview** (trimmed to 2000 chars; `server/a2a.py`). To read the whole
report the user had to leave the conversation for the Background-agents panel / Activity feed.
The Activity feed (ADR 0003/0022) has the same problem at scale — a long entry is a wall of
markdown inline in a narrow utility-bar dialog, with no comfortable full read.

Both are the same need: **read a long document, full-screen, without losing your place.** And
it recurs — long tool outputs, knowledge docs, generated artifacts, PR bodies all want it. So
the answer should be a *reusable surface*, not a one-off "expand report" dialog.

## Decision

A single, app-wide **document viewer**: `openDocument(spec)` opens a root-mounted full-screen
`<DocumentViewer/>` (DS `Dialog`, tall + scrolling body, console `Markdown` renderer). Mirrors
the context-menu system's store+host+imperative-open pattern (`src/docviewer/`, ~zustand store).

`DocumentSpec` is deliberately generic so any feature opens the **same** reader; body resolves
in priority order:

1. `render()` — an arbitrary React body (escape hatch for non-markdown future views),
2. `load()` — async markdown fetched on open (e.g. the FULL report by job id), then
3. `content` — inline markdown.

Plus `title` + optional `subtitle`. Ephemeral (never persisted; a refresh closes it).

**First two consumers** (proving the seam):
- **Chat background-report card** — `ChatMessage.report = {jobId, title}` (set by `BackgroundWatch`
  from the `background.completed` event). The bubble keeps the server preview; a **"Read full
  report"** button calls `openDocument({ load: fetch the full result by jobId })` — so the reader
  shows the *true* full report, not the 2000-char preview.
- **Activity feed** — each entry gets an "open in reader" affordance → `openDocument({ content:
  entry.text })`. Same viewer the chat card opens.

## Consequences

- **One reader, many openers** — a new "open full-screen" affordance is an `openDocument()` call,
  not a bespoke dialog. The chat card and Activity already share it; future long-content views
  (tool output, knowledge, artifacts) plug in with zero viewer changes.
- **Reuses the DS** — DS `Dialog` (overlays) + `@protolabsai/ui/markdown` (via the console
  `Markdown`/`LazyMarkdown` wrapper), so reports/docs render identically wherever they're read.
- **DS gap to contribute back:** `Dialog` has no `size="fullscreen"` — the tall reading surface is
  achieved with a `width` + a `.doc-viewer` className override (`docviewer.css`). A first-class
  fullscreen/size variant on `Dialog` would remove the CSS override.

## References

- ADR 0050 (background subagents — the report source), ADR 0003/0022 (Activity feed), ADR 0037
  (DS foundation — `Dialog`, markdown renderer), ADR 0036 (the store+host+imperative-open pattern
  this mirrors). Module: `apps/web/src/docviewer/`.
