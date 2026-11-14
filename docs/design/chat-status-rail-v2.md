# Chat status rail v2: a quiet work-chip popover, and people on the right

**Status:** approved by Josh, 2026-11-14, on the v4 preview. Ready to card. Nothing is built yet.
**Revises:** the S6b row in the Work consolidation spec (third pass, `docs/design/work-consolidation.md`), and the grouped details panel in `apps/web/src/chat/ChatStatusRail.tsx` and `chat-status-rail.css`.
**Preview:** artifact `a-1794690588775-35a88f`, **version 4** (the approved one). It shows every group at 1440 and 390 px, in light and dark, and passes its layout check. Later versions (v5) explored extra cuts that Josh did not adopt; build from v4 and this document.
**Owner:** the designSystem agent (design); protoEngineer (cards and build).

## Decisions

Josh decided these on 2026-11-14.

| # | Question | Decision |
|---|---|---|
| D1 | Does each chip open only its own group? | **Yes.** One group per chip. |
| D2 | What is the one quiet line under each row? | **The age or the next time** ("12 min", "Set today", "Next at 21:30"). Not a status word. |
| D3 | What does clicking a row do? | **Opens that item's Work detail and closes the popover.** |
| D4 | Does Needs you keep in-place answering? | **Yes.** The first decision keeps its answer buttons; stopped goals keep Keep going and Drop the goal. |
| D5 | Where do the people (delegates) sit? | **On the right edge of the rail, as in v1.** Work chips stay on the left. |

## 1. The work-chip popover

### The rule

A popover holds three things: a heading line, the rows, and one "Open in Work". Nothing else, except the Needs you answer controls (D4). The old problems (hard truncation, the stray "ap…" meta label) are fixed by letting the text wrap, not by adding chrome.

### One group per chip (D1)

Each chip opens a panel with only its own group:

| Opener | Panel content |
|---|---|
| Needs you marker | Needs you only |
| Doing now chip | Doing now only |
| Coming up chip | Coming up only |
| Told it chip | Your instructions only |
| `+K` chip | The people picker ("In this chat · N"), unchanged |

Today `DetailsPanel` renders every group, plus the Needs you section and (while names are hidden) the People section, in every panel. v2 removes both riders: the Needs you marker is always on the rail, and `+K` is always on the rail when names are hidden, so neither needs to repeat inside another group's panel.

### Heading

One line: **"Your instructions · 1"**.

- The group name: `--pl-font-size-sm`, weight 600, `--pl-color-fg`.
- The " · 1": weight 400, `--pl-color-fg-muted`, `aria-hidden`.
- The `h2`'s accessible name: "Your instructions, 1". The section's `aria-label` matches.
- No count badge and no scope label.
- The heading uses Work's group name (`WORK_GROUP_LABELS`). The rail chip still says "Told it".
- When the count is unknown (the ledger never loaded), the heading is the name alone. Never claim "0".

### Rows

Each row has two lines and nothing else:

1. **The item text.** `--pl-font-size-sm`, `--pl-color-fg`. It wraps and is clamped to **2 lines** (`-webkit-line-clamp: 2`, `overflow-wrap: anywhere`), then ends in an ellipsis. The full text is in the row's accessible name and a `title` tooltip.
2. **One quiet line (D2).** `--pl-font-size-xs`, `--pl-color-fg-subtle`, one line, ellipsized. It is the item's age or next time. See "The quiet line, per kind" below.

What a row does **not** have: no status dot, no chevron, no per-row action button, no `meta` column (the old `chat-status-details__meta` split is gone), and no divider between rows. Rows are separated by spacing. A row gets `--pl-color-bg-hover` on hover and the DS focus ring on `:focus-visible`.

**Click target (D3).** The whole row is one `<button type="button">` with two text children. That is the composite-row case allowed by protoContent#551 (raw `<button>`, DS focus-visible ring, tokens only). Clicking it:

1. closes the popover (or the phone sheet), then
2. calls `openWorkGroup(group.id, "<kind>:<id>")`, which already sets `surfaceStates.work.itemId` and the `thisChat` visit scope.

The row's accessible name: "&lt;item text&gt;. &lt;quiet line&gt;. Open in Work".

**Scroll.** The row list scrolls on its own at `max-height: min(60vh, 360px)`. The heading and "Open in Work" stay outside the scroller, so the link is always reachable in a long group. There is no cap on the number of rows.

### The quiet line, per kind

The line is the age or the next time, never a status word. If the source carries no usable timestamp, the row shows the text line only. An empty cue is better than an invented one.

| Group | Kind | Quiet line | Source |
|---|---|---|---|
| Your instructions | directive | "Set " + age, for example "Set today at 14:02", "Set 13 Nov at 09:00" | `DirectiveRow.created_at` via `relTime()` |
| Doing now | background job | "Started " + age, for example "Started 12 min ago" | the job's start time; the builder confirms the field on the background-job type. No field means no line. |
| Doing now | active goal | "Step 3 of 8" (or "Step 3" with no max) | `GoalState.iteration`, `max_iterations`. The goal's progress is its age equivalent; v4 shows it. |
| Doing now | in-progress or blocked task | "Updated " + age; past 24 h, `staleTaskMeta()` ("No activity for 2 days") | `Task.updated_at` / `created_at`, `taskStaleDays()` |
| Coming up | schedule or wait | "Next " + next time, for example "Next at 21:30", "Next tomorrow at 14:00", "Next 18 Nov at 09:00"; a paused one reads "Paused" | `ScheduledJob.next_fire` |
| Coming up | watch | "Since " + age | `WatchState.created_at` (epoch seconds) |
| Coming up | open or deferred task | "Added " + age | `Task.created_at` |
| Needs you | decision | agent + age, for example "designSystem · asked 5 min ago" | `DecisionRow.agent`, `created_at`. Needs you counts every chat, so the agent name is the scope cue. |
| Needs you | waiting task | "Waiting on you · " + age | `Task.updated_at` |
| Needs you | undelivered page | source + "couldn't be delivered" (unchanged) | `InboxItem.source` |
| Needs you | stopped goal | "Goal stopped" (unchanged), with its buttons | — |

`relTime()` only reads the past ("12 min ago", "today at 14:02"). The "Next …" line needs a small future formatter beside it in `work/tone.ts`, with the same `en-GB` time and day style and the same tooltip shape. Never use a warning colour on this line, including for stale jobs.

### Open in Work

One quiet DS `Button`: ghost, `sm`, label "Open in Work", left-aligned under the rows (its padding hangs into the panel's padding so its text lines up with the row text). No divider, no chevron. It calls `openWorkGroup(group.id)` without an item.

### Needs you (D4)

Needs you keeps the S7 content and its controls, inside the v2 frame:

- The same heading line: "Needs you · 2".
- The **first pending decision** stays the shared `DecisionCard`, expanded, with its answer buttons and the "Still waiting" hint past 30 minutes. Answered cards are still held in the open panel until it closes (§5.5 hold).
- Every **other decision** becomes a quiet row (text = the question, quiet line = agent + age). Clicking it expands it into a `DecisionCard` in place, as today. This is the one row that doesn't navigate: answering in place is the point of D4.
- **Stopped goals** keep Keep going and Drop the goal, with the inline confirm.
- **Waiting tasks** become quiet rows that open their Work detail (D3). The per-row "Open in Work" button goes.
- **Undelivered pages** become quiet rows that open the Needs you group in Work.
- The panel ends with the one "Open in Work", unscoped (Needs you is never chat-scoped).

### States

| State | What shows |
|---|---|
| Loading | The heading without a count, then two `pl-skel` lines, `aria-busy="true"`. |
| Couldn't load | One muted sentence, "Couldn't check your instructions in this chat.", a DS `Button` (`sm`) "Load again", then "Open in Work". No count is claimed, on the chip or in the heading. |
| Out of date | The rows stay, then one muted line "Your instructions may be out of date." and "Load again". |
| Emptied while open | The panel stays open with one `role="status"` line: "Nothing running in this chat now." (Doing now), "Nothing coming up in this chat." (Coming up), "No instructions in force in this chat." (Your instructions). It closes when the operator dismisses it, never under the cursor. This replaces today's `openGone` auto-close for work chips; the `+K` picker keeps its current behaviour. |
| Long group | The rows scroll inside the panel. |

### Desktop and phone

- **Desktop:** DS `Popover`, `side="top"`, `align="start"`, inside the DS 320 px cap (`.pl-popover`). No DS change is needed. `trapTab` stays.
- **Phone (under 768 px):** DS `Drawer`, `side="bottom"`, with `title` set to the heading text so the close button renders. Rows are at least 48 px tall. "Open in Work" and "Load again" are full width and at least 44 px tall. Needs you answer buttons keep `md` size, stacked full width, as today.

### Accessibility

- Each panel is a dialog named by its heading ("Doing now, 2").
- Focus returns to the opening chip on every close, as today.
- Row buttons are in the tab order in list order, then "Open in Work".
- The rail keeps exactly one polite live region (§6). The "emptied while open" line is `role="status"` inside the open panel only.
- Contrast: row text is `--pl-color-fg` on `--pl-color-bg-raised`; the quiet line is `--pl-color-fg-subtle`, the same pairing Work uses for meta text. It is supplementary: the item text carries the meaning.

## 2. People on the right (D5)

The rail stays one row with two groups:

- **Work group (left):** the Needs you marker, then the group chips. `flex: none`. These never fold.
- **People group (right):** the `.chat-roster` box: the Users icon, the visible `@name` chips, then `+K`. It keeps `flex: 1 1 auto; min-width: 0; overflow-x: clip; overflow-y: visible`.

How it moves right: `margin-inline-start: auto` on the roster's first child, the Users icon (`.chat-status-rail .chat-roster > svg:first-child`). **Do not** use `justify-content: flex-end`. An auto margin collapses to zero when the row overflows, so a chip that hasn't been refitted yet is clipped on the right, as today. With `flex-end` the overflow would clip the icon and the first, most important names instead.

The gap between the two groups is `--pl-space-4` (the rail's `gap` between the work chips and the roster), so they never touch at the tightest fit. Gaps inside each group stay `--pl-space-1_5`.

What does not change:

- The S6b measurement: the roster's `ResizeObserver` width is still "what the work chips leave", and the hidden mirror row is measured the same way. `fitCount()`, `orderPeople()`, `fallbackCap()` and `spokenMoreLabel()` are untouched. The mirror row is `position: absolute; left: 0`, so the auto margin on the live icon doesn't affect it.
- Order inside the people group: draft-addressed names first (the latest picker add leading them), then cast order. `+K` is always last, on the right edge.
- The `+K` popover keeps `align="end"`. Work-chip popovers keep `align="start"`.
- DOM order and focus order: work first, then people.

**Phone width.** Work chips show numbers only, as today. The people group usually shrinks to the Users icon and `+K` on the right edge (the fallback cap allows one name beside work chips and none beside the Needs you marker). With no work chips, the people get the whole row and still sit on the right.

## Out of scope

- No design-system (protoContent) change. Everything uses existing `@protolabsai/ui` components (`Popover`, `Drawer`, `Button`, `Skeleton` classes) and `--pl-*` tokens.
- No change to the Work view itself, to the chip labels or counts, or to `workGroups()`.
- The Other conversations work is a separate thread.

## Build slices

Three console slices in protoAgent (`apps/web`). They share `chat-status-rail.css`, so they are chained: each one depends on the one before. Every slice runs the repo's real gate (typecheck, `vitest`, the `chat-css-tokens` token guard). The PRs say **Refs** this spec; there is no source issue.

### Slice 1 — People on the right

Small. CSS only, plus a test.

- **Files:**
  - `apps/web/src/chat/chat-status-rail.css`
  - `apps/web/src/chat/ChatStatusRail.people.test.tsx`
- **Change:** the rail gap between the work group and the roster becomes `--pl-space-4` (gaps inside each group stay `--pl-space-1_5`); `margin-inline-start: auto` on the roster's leading Users icon. Nothing else.
- **Done when:**
  - With work chips and people, the people sit at the right edge of the rail at 1440 and 390 px.
  - The measured `+K` collapse still works: existing people tests pass unchanged.
  - A test asserts the roster's first child is the Users icon and the `+K` chip is its last visible chip (the structure the CSS relies on).
  - No hardcoded values; the token guard passes.

### Slice 2 — Quiet popover for Doing now, Coming up and Your instructions

Medium. Depends on slice 1.

- **Files:**
  - `apps/web/src/chat/ChatStatusRail.tsx`
  - `apps/web/src/chat/chat-status-rail.css`
  - `apps/web/src/chat/ChatStatusRail.test.tsx`
  - `apps/web/src/work/tone.ts` (the future-time formatter and the per-kind quiet line)
  - `apps/web/src/work/tone.test.ts`
- **Change:**
  - D1: a work chip's panel renders only its own `GroupSection`. Remove the Needs you rider and the People rider from work-chip panels. The phone `Drawer` shows the opened group only, titled with its heading.
  - The heading line, the quiet rows (2-line clamp, one quiet line), and one "Open in Work" button, as specified above. Remove `chat-status-details__meta` from these rows.
  - D3: the row button closes the panel and calls `openWorkGroup(group.id, "<kind>:<id>")`.
  - D2: a pure `railCue(item, now)` (or similarly named) in `tone.ts` returns the quiet line per the table, or `""`. A `nextTime()` formatter sits beside `relTime()`.
  - The states table, including "emptied while open" replacing the auto-close for work chips.
  - Rows scroll at `max-height: min(60vh, 360px)`; heading and link stay outside the scroller.
- **Done when:**
  - Each work chip's panel holds exactly one group section; no Needs you or People section appears in it.
  - A long item's text is in the DOM in full (clamped by CSS) and in the row's accessible name and `title`.
  - Clicking a row closes the panel and calls `openWorkGroup` with that item's `kind:id`; "Open in Work" calls it with no item.
  - `railCue` unit tests cover every kind in the table, the stale-task case, a missing timestamp (returns `""`), and today, tomorrow and later dates for "Next …".
  - Loading, couldn't load, out of date and emptied-while-open render as specified; couldn't load never shows a count.
  - Focus returns to the opening chip on close; the token guard passes.

### Slice 3 — Needs you in the quiet frame

Medium. Depends on slice 2.

- **Files:**
  - `apps/web/src/chat/NeedsYouSection.tsx`
  - `apps/web/src/chat/ChatStatusRail.tsx` (the marker's panel shows Needs you only)
  - `apps/web/src/chat/chat-status-rail.css`
  - `apps/web/src/chat/NeedsYouSection.test.tsx`
  - `apps/web/src/chat/ChatStatusRail.decisions.test.tsx`
- **Change:**
  - The marker's panel renders `NeedsYouSection` alone, with the v2 heading line and one "Open in Work" (unscoped).
  - D4: the first pending decision stays the shared `DecisionCard` with its answer buttons, the stale hint and the §5.5 hold. The other decisions become quiet rows (question + "agent · asked &lt;age&gt;") that expand in place on click. Stopped goals keep Keep going / Drop the goal with the confirm.
  - Waiting tasks become quiet rows that open their Work detail (`openWork("task:<id>")`), replacing the per-row "Open in Work" button. Undelivered pages become quiet rows that open the Needs you group.
  - `NEEDS_YOU_SECTION_KINDS` and the marker count don't change.
- **Done when:**
  - The marker's panel holds only the Needs you section.
  - Answering the first decision in the panel still works through the shared decisions store and the result is held until the panel closes (existing decisions tests pass).
  - A second decision renders as a quiet row and expands into a `DecisionCard` on click.
  - A waiting task row opens its Work detail and closes the panel; no per-row button remains.
  - Keep going and Drop the goal still work, with the confirm step.
  - The token guard passes.
