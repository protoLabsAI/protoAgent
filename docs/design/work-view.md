# Work view — design spec

> **Superseded in part (2026-11-12):** the group structure here (§2.1 Overview lanes and the eight sections) is replaced by [work-consolidation.md](./work-consolidation.md). Row, tone, inspector, confirm and undo rules still apply.

Status: approved by Josh, 2026-11-09. Tracking: #4955.

Third pass (layout) approved by Josh 2026-11-11. Preview: designSystem artifact a-1794389235320-cdc47b.

## 1. What this view answers

This view answers one question at a glance: **what is this agent doing, what is it waiting on, what will it do next, and what did it just do?**

It also answers a second question first: **does anything need me?** Pending decisions sit at the top, above everything else.

It covers every kind of agent work in one place: decisions, goals, watches, tasks, schedules, background agents, activity and directives. Each kind keeps its own section with its own detail view. One status vocabulary and one row layout serve them all. The view shows what the agent's records say. It never guesses a status the backend didn't report.

## 2. Information architecture

### 2.1 Overview lanes

The Overview has **five lanes**. They are listed in this order, and an empty lane is hidden:

1. **Needs you**: pending decisions, pinned first.
2. **Doing**: running goals, in-progress tasks, running background jobs.
3. **Waiting on**: armed watches, blocked tasks, goals waiting on a verifier.
4. **Next**: enabled schedules and open tasks. A count of disabled schedules links to the Schedules section (Q3).
5. **Done**: the last 24 h of finished items and activity, with **Show more** (Q2).

Sections: Overview · Decisions · Goals · Watches · Tasks · Schedules · Background agents · Activity · Directives (Q11).

Work always shows every chat. There is no This chat / All filter (Josh, 2026-11-11, #5083). Where the source chat matters, the row meta names it.

### 2.2 Shared tone map

Every item type maps its raw status onto one of eight statuses. The tone shows how urgent an item is. The word shows what state it is in. Colour is never the only signal.

| Status | Tone (`Status`) | Token | Dot | Lane |
|---|---|---|---|---|
| needs you | `warning` | `--pl-color-status-warning` | steady | Needs you |
| running | `info` | `--pl-color-status-info` | `pulse` | Doing |
| waiting | `neutral` | `--pl-color-fg-subtle` | steady | Waiting on |
| scheduled | `neutral` | `--pl-color-fg-subtle` | steady | Next |
| paused | `neutral` | `--pl-color-fg-subtle` | steady | Next (Schedules section only for disabled schedules, Q3) |
| done | `success` | `--pl-color-status-success` | steady | Done |
| failed | `error` | `--pl-color-status-error` | steady | Done (also counted on the Overview header) |
| cancelled | `neutral` | `--pl-color-fg-subtle` | steady | Done |

Rules:
- Tone is carried by `StatusDot` and `Badge` only. **Row text never uses a status token as its text colour.** In light theme, `status-success` (3.41:1), `status-warning` (3.30:1) and `status-info` (4.21:1) fail 4.5:1 as text on `--pl-color-bg-raised`. As dots they pass the 3:1 non-text minimum (WCAG 1.4.11) in both themes. The lowest pair is warning in light theme, at 3.30:1.
- Meta text uses `--pl-color-fg-muted` (7.24:1 dark, 7.73:1 light). It never uses `--pl-color-fg-subtle`, which is 3.42:1 on light.
- `neutral` covers four statuses. They are told apart by the Badge word, not by colour.
- This map replaces the six per-panel tone maps.
- The build confirms that the neutral tone of `StatusDot` and `Badge` resolves to `--pl-color-fg-subtle`. If it doesn't, the component's own neutral tone is still correct, and only this table changes.

### 2.3 Shared row and detail rules

- Every timestamp shows as local relative time: "2 min ago", "today at 17:14", "7 Nov at 17:14". The full local timestamp goes in a `Tooltip`. Epoch seconds (goals, watches) and ISO strings (everything else) render the same way.
- **Technical details** (Q4): a collapsed, copyable block at the bottom of every detail view. It is the only place where ids, raw statuses, verifier types, cron strings, backend names and session ids appear. It is never shown in a row.
- A field with an empty value hides its line. It never shows "—" or "none".

### 2.4 Decisions

The card design is unchanged (decision-needed.md, revision 1). This table lists only what the row and the Overview pin use. Amendment A1 is placement only.

| Order | Field | Source | Shown as |
|---|---|---|---|
| 1 | Question | `question` | Row title, wrapping to 2 lines. Full text on the card. |
| 2 | Status | `state` | pending → **needs you**; answered → **done**; expired, cancelled, withdrawn → **cancelled** |
| 3 | Asked | `created_at` | "Asked 4 min ago" |
| 4 | Expiry and default | `expires_at`, `default_action_text` | "If no answer by 18:00: {default_action_text}". Hidden when there is no expiry. |
| 5 | Agent | `agent` | Shown across all chats, for example "protoEngineer" |
| 6 | Outcome (closed) | `answer_label`, `answered_by`, `answered_at`, `withdraw_reason` | "Answered: {answer_label} · by you · 17:20", or "Withdrawn: {reason}" |

**Never shown:** `id`, `session_id`, `thread_id`, `interrupt_id`, `default_option` (as an id), `answered_surface`, the raw `state` string. Technical details: `id`, `session_id`, `thread_id`.

### 2.5 Goals

| Order | Field | Source | Shown as |
|---|---|---|---|
| 1 | Goal | `outcome`, or else `condition` | Row title |
| 2 | Status | `status` | in progress → **running**; between turns waiting on a verifier → **waiting**; achieved → **done**; exhausted, unachievable → **failed**; cleared → **cancelled** |
| 3 | Progress | `iteration`, `max_iterations` | "Turn 3 of 10". With no limit: "Turn 3". Never "n/0" or "∞". |
| 4 | Stalled | `no_progress_streak`, `no_progress_limit` | "No progress for 2 of 3 checks". Only when the streak is above 0. |
| 5 | Latest check | `last_reason` | One line: "Last check: {last_reason}" |
| 6 | Started and finished | `started_at`, `finished_at` | "Started 10 min ago" · "Finished today at 17:14" |
| 7 | Stop rule (detail) | `stop_when` | "Stops and asks you if: …" |
| 8 | Constraints and scope (detail) | `constraints`, `boundaries` | Two short bullet lists: "Must not change", "In scope" |
| 9 | Why it stopped (detail) | `abandon_reason` | "The agent stopped: …" |
| 10 | History (detail) | `history[]` | A timeline with one row per turn: "Turn 3 · kept going · {reason}". The terminal status reads "achieved", "ran out of turns" or "couldn't be done". |
| 11 | Fresh context (detail) | `fresh_context` | "Each turn starts a clean thread". Only when true. |

**Never shown:** session id, the raw `verifier.type`, the raw `last_evidence`, raw history `status` strings (`continue` and so on), the raw `evidence` per turn. Technical details: session id, verifier type and arguments, `last_evidence`, per-turn `evidence`.

### 2.6 Watches

| Order | Field | Source | Shown as |
|---|---|---|---|
| 1 | Condition | `condition` | Row title |
| 2 | Status | `status` | active → **waiting**; met → **done** (a `repeat` watch that fired stays **waiting**); expired, cleared → **cancelled** |
| 3 | Kind | `trigger`, `repeat` | "Fires once when met" · "Fires every time it's met" · "Fires when the value changes" |
| 4 | Last checked | `last_checked`, `check_count` | "Checked 2 min ago · 41 checks" |
| 5 | Fired | `fire_count`, `consecutive_fires` | "Fired 3 times". With 3 or more fires in a row, it adds "· 3 in a row", the flapping signal. |
| 6 | Then | `run_prompt` | "When met, the agent will: {run_prompt}". 2 lines in the row, full text in the detail view. |
| 7 | Deadline | `deadline` | "Until 18:00" or "Expired today at 18:00" |
| 8 | Latest check (detail) | `last_reason` | "Last check: …" |
| 9 | Cadence (detail) | `interval_s`, `stall_after` | "Checks every 5 min" · "Tells you after 6 checks with no change" |
| 10 | Created and finished (detail) | `created_at`, `finished_at` | "Set 1 h ago" · "Met today at 17:14" |

**Never shown:** `id`, `verifier.type`, `run_session`, the raw `last_evidence`, the raw `status`. Technical details: `id`, verifier type and arguments, `run_session`, `last_evidence`.

### 2.7 Tasks

| Order | Field | Source | Shown as |
|---|---|---|---|
| 1 | Title | `title` | Row title |
| 2 | Status | `status` | in_progress → **running**; blocked → **waiting**; open → **scheduled** (the detail view says "Not started"); deferred → **paused**; closed → **done** |
| 3 | Type and priority | `issue_type` or `type`, `priority` | "Bug · high". Priority 0 → urgent, 1 → high, 2 → normal, 3 → low. |
| 4 | Assignee | `assignee` | "Assigned to {name}" |
| 5 | Updated | `updated_at` | "Updated 5 min ago" |
| 6 | Closed | `closed_at` | "Closed today at 17:14". Only when closed. |
| 7 | Description (detail) | `description` | Markdown, clamped to 8 lines with **Show full text** |
| 8 | Created (detail) | `created_at` | "Created 7 Nov at 09:00" |

**Never shown:** `id`, the raw `status`, the raw numeric `priority`. Technical details: `id`.

### 2.8 Schedules

| Order | Field | Source | Shown as |
|---|---|---|---|
| 1 | What it does | `prompt` | Row title, first line, 2 lines at most. **Never the job id.** |
| 2 | Status | `enabled`, `next_fire`, `last_fire` | enabled with `next_fire` → **scheduled**; `enabled: false` → **paused**; a one-shot that has fired → **done** |
| 3 | When | `schedule`, `timezone` | Plain words: "Weekdays at 09:00", "Once, 10 Nov at 14:00". The timezone is shown only when it differs from local time. |
| 4 | Next run | `next_fire` | "Next: in 2 h (today at 17:00)" |
| 5 | Last run | `last_fire` | "Last ran yesterday at 09:00". Links to that turn in Activity when it exists. |
| 6 | Agent | `agent_name` | Shown across all chats |
| 7 | Created (detail) | `created_at` | "Created 7 Nov" |

**Never shown:** `id`, the raw cron `schedule`, ISO timestamps, the backend name, the IANA timezone string. Technical details: `id`, cron string, IANA timezone, backend.

Disabled schedules show only in the Schedules section. The Overview shows "{N} paused schedules" under Next and links to the section (Q3).

### 2.9 Background agents

| Order | Field | Source | Shown as |
|---|---|---|---|
| 1 | Task | `description` | Row title |
| 2 | Status | `status` | running → **running**; completed → **done**; failed → **failed**; canceled → **cancelled** |
| 3 | Role | `subagent_type` | Display name: "Researcher", "Design critic" |
| 4 | Time | `created_at`, `completed_at` | "Started 3 min ago" · "Took 4 min" |
| 5 | Why it ended | `error` | "Stopped: the server restarted before the job finished". Not shown for a completed job. |
| 6 | Result (detail) | `result` | Markdown, clamped to 8 lines with **Show full text** |
| 7 | From chat | `origin_session` | "From {chat title}", as a link. Never the raw id. |

Below the list, a link reads **Manage subagents**. It opens `SubagentsPanel` (Q14).

**Never shown:** `id`, `origin_session` as an id, the raw `subagent_type` slug. Technical details: `id`, `origin_session`, `subagent_type`.

### 2.10 Activity

| Order | Field | Source | Shown as |
|---|---|---|---|
| 1 | What the agent said | `text` | Row title, 2 lines. Full text in the detail view. |
| 2 | Status | `state` | finished → **done**; errored → **failed** (the raw values are confirmed in the build) |
| 3 | Triggered by | `origin`, `trigger` | "Scheduled: Weekday standup" · "Inbox: GitHub" · "Webhook" · "Another agent" · "You" |
| 4 | When | `created_at` | "12 min ago" |
| 5 | In response to (detail) | `stimulus` | Block quote: "In response to: …" |
| 6 | Priority | `priority` | "Urgent" only for the inbox `now` tier. Otherwise hidden. |

Activity shows the last 24 h, with **Show more** (Q2).

**Never shown:** `id`, `task_id`, the raw `origin` and `state`, the job id inside `trigger` when a human label exists. Technical details: `id`, `task_id`, raw `trigger`.

### 2.11 Directives

This section keeps the approved ledger design. Its fields, "Never shown" list and copy are the ones in directive-ledger-view.md §2 and §4 (Q11). It is the one exception to §2.2: an in-force entry keeps the ledger's word **"Active"** and a `neutral` tone, and is not mapped to running. Closed entries map to **done** (fulfilled) and **cancelled** (revoked) only in Overview counts.

## 3. Placement

### 3.1 Rail and modal: the console-wide surface pattern

This is defined once, at the shell level. Work is the first view to use it, and any other rail view can adopt it the same way.

- **One host, two frames.** A shell surface host renders any registered view in the **rail** (right dock) or the **modal** (large dialog). On the phone, both frames become a full-screen sheet (Q6), so the controls are hidden there.
- **Controls.** The rail header has **Expand**, which opens the same view in the modal. The modal header has **Dock to rail**. Every view gets both, even a view with no state to keep. Double-click does not expand the rail, and its menu has no **Open in window** action.
- **Keeping your place.** The host moves a view's `SurfaceState` between frames: `section`, `itemId`, `scrollTop` and `filters`. Each view decides what goes in it. The host stores and restores it, and never reads it.
- **Deep link.** The deep link is `<view>/<section>[/<item-id>]` (here `work/…`). It opens in whichever frame the entry point asks for. Utility-bar pills and palette commands open the modal. Chat-strip "View" buttons open the rail.
- **Focus.** After a move, focus lands on the same item, or on the section heading if no item is open. A screen reader hears "Expanded" or "Docked to rail".

### 3.2 Shared layout (every frame)

The list is the page. The detail opens only when you pick an item; no pane exists for nothing.

- **Section picker:** `SideNav collapsed ariaLabel="Work sections"` with raw number `badge` counts. It is the first line of the list, left-aligned and sized to its label (`flex: 0 1 auto`), never a full-width bar. (DS follow-up: protoContent#674 adds `fit="content"`.)
- **Lane and section headings:** `h2`, label plus `Count` ("Doing 2"), `--pl-font-size-sm`, `--pl-font-weight-semibold`, smaller than the frame title. The accessible name includes the count ("Doing, 2").
- **Help line:** only while one lane is visible; copy from §4.2; `--pl-color-fg-muted`, `--pl-font-size-sm`.
- **Lane list:** one `ul` per lane as a single bordered group (`--pl-color-bg-raised`, `--pl-border-width` solid `--pl-color-border`, `--pl-radius-lg`, `overflow: hidden`). Rows are separated by a hairline border, with no gap between rows. Lanes are `--pl-space-4` apart.
- **Row (`WorkRow`):** one button; padding `--pl-space-2` × `--pl-space-3`; title clamps at 2 lines; meta one line with ellipsis; hover `--pl-color-bg-hover`; the open item has `aria-current="true"` and `--pl-color-bg-subtle`. When the list is at least 40rem wide (container query on the list), the title and meta sit side by side on one line (3fr / 2fr, both truncated, title clamps to 1 line). Target: about 44px for a one-line row.
- **Empty:** `Empty` with title and description, placed inline in the list flow under the picker and left-aligned. It never floats in the middle of a pane. Goals keep **New goal**. (DS follow-up: protoContent#673 adds `Empty align="start"`.)

### 3.3 The rail (right column)

- It mounts in the `AppShell` right column, inside the 280–720 px resize range. Single column.
- Opening a row pushes its detail view in place of the list, with a **Back to {section}** ghost button. The list keeps its scroll position.
- Chat strips open the rail on their section (DirectiveStrip → Directives, GoalRunStrip → Goals, BackgroundWorkStrip → Background agents).

### 3.4 The window

- `Dialog size="wide" height="fill" padding="none"`, clearing the app header (#5079). Header: title **Work**, **Dock to rail**, Close.
- **Nothing selected:** one column that fills the window. The list content is capped at `64rem` and left-aligned. There is no detail pane and no "Pick an item" placeholder.
- **Item open:** an inspector pane appears on the right at `clamp(24rem, 38%, 34rem)` with a start border (`--pl-border-width` `--pl-color-border`). The list stays visible with the open row marked current. The inspector has a ghost icon button **Close details** (✕, `aria-label` + `Tooltip`) instead of Back. Escape closes the inspector first, then the dialog. Focus returns to the row that opened it.
- **Below 48rem of dialog width** (container query, not viewport) it behaves like the rail: push and Back.

### 3.5 The phone sheet (390 px)

- `Drawer side="bottom" fullScreen` titled **Work**, with no Expand or Dock to rail. The body is the rail layout (§3.3).
- Rows are at least `--pl-space-12` tall.
- Detail actions are `size="md"`, full width and stacked, destructive last. Back to {section} is `size="md"`.

### 3.6 Utility-bar pills

All pills stay (Q12). Each opens the **modal** on its section and keeps its badge.

| Pill | Opens | Badge |
|---|---|---|
| Settings | Settings (unchanged) | — |
| Palette | Command palette (unchanged) | — |
| Decisions | Work › Decisions | `Count` of pending decisions. Hidden at 0. |
| Background jobs | Work › Background agents | A pulsing dot while any job runs, plus a `Count` of running jobs |
| Activity | Work › Activity | `Count` of unseen items since last opened. Hidden at 0. |
| Plugin `UtilityWidget`s | Unchanged (Q13) | Unchanged |

Work gets **no new pill**. You reach it from the right-rail icon, the palette, and the three pills above.

### 3.7 Palette entries

Every palette entry opens the **modal**.

| Palette row | Opens |
|---|---|
| Open Work | Work › Overview |
| Work: Decisions / Goals / Watches / Tasks / Schedules / Background agents / Activity / Directives | That section |
| Work: New goal… | Work › Goals, with the create form open (Q15) |
| `/directives` | Work › Directives. It replaces the old drawer; non-console clients still get the text reply. |

## 4. Copy

Plain words only. No raw ids, cron strings, verifier types or API status values outside **Technical details** (Q4). Status words come from the §2.2 tone map; badges render lowercase.

### 4.1 Frame and navigation

| Element | Text |
|---|---|
| View title | Work |
| Section names | Overview · Decisions · Goals · Watches · Tasks · Schedules · Background agents · Activity · Directives |
| Section trigger (`aria-label`) | Work sections |
| Subagents link | Subagents ↗ |
| Expand / Dock | Expand / Dock to rail |
| Back | Back to {section} |
| Close inspector (window) | Close details |

### 4.2 Overview lanes

Headings use label plus `Count`, for example "Doing 2", with accessible name "Doing, 2" (§3.2).

| Lane heading | Help line (only while it is the only visible lane) |
|---|---|
| Needs you {N} | Decisions waiting for your answer. |
| Doing {N} | Running now. |
| Waiting on {N} | Paused until something happens. |
| Next {N} | Scheduled or queued. |
| Done {N} | The last 24 hours. |
| Disabled schedules (Next lane foot) | {N} disabled schedules · View |
| Older items | Show more |

### 4.3 Item actions (detail view)

| Action | Button | Shown when |
|---|---|---|
| Pause / Resume | Pause / Resume | the backend supports pause (Q8) / the item is paused |
| Edit | Edit | the backend supports edit (Q9). Schedules today. |
| Run now | Run now | schedules |
| Cancel | Cancel… | goals, background jobs |
| Delete or clear | Delete… | goals, watches, tasks, schedules |
| Retry | Retry | any error state |
| Copy | Copy | inside Technical details |
| Technical details | Technical details | always, collapsed |

### 4.4 Inline confirm (same pattern as the directive ledger)

Destructive actions confirm **inline in the detail view**, not with `ConfirmDialog` (no stacked overlays).

| Type | Prompt | Explanation | Confirm | Keep |
|---|---|---|---|---|
| Goal | Delete this goal? | The agent stops working toward it. Work it has already done stays. | Delete | Keep it |
| Goal (cancel) | Stop this goal? | The agent stops after its current step. | Stop | Keep going |
| Watch | Delete this watch? | The agent stops checking. Nothing runs when the condition is met. | Delete | Keep it |
| Task | Delete this task? | It leaves your task list. | Delete | Keep it |
| Schedule | Delete this schedule? | It won't run again. A run already in progress finishes. | Delete | Keep it |
| Background job | Cancel this job? | The job stops now. Any partial result is discarded. | Cancel job | Keep running |

### 4.5 Results and announcements

| Case | Visible text | Where | Announced |
|---|---|---|---|
| Deleted, undo available | {Type} deleted. · Undo | Toast (neutral), about 8 s (Q10) | "{Type} deleted. Undo is available for a few seconds." |
| Undone | Restored. | Toast | "Restored." |
| Undo too late | Couldn't restore it. The undo window has closed. | Toast (warning) | same |
| Paused / resumed | Paused. / Resumed. | Status line in the detail view | polite |
| Action failed | Couldn't {verb} it. Nothing changed. · Try again | Inline `Alert status="warning"` | assertive |
| Expanded | — | — | "Work opened in a window." |
| Docked | — | — | "Work docked to the side rail." |
| Section change | — | — | "{Section}, {N} items." |
| New "Needs you" item while open | — | — | "New decision: {title}." (polite, once) |
| Open item changed | This item changed while you were looking. · Refresh | `Callout tone="info"` | polite |

## 5. States

These apply to every section and every Overview lane. A section shows its own state. One failed source never blanks the whole view.

### 5.1 Loading
- First load: three `Skeleton` rows under each lane or section heading. Counts, badges and Empty states wait until the first load finishes, so the view never flashes "0" or "Nothing here".
- Moving between frames is not a reload: the data stays and no skeleton shows.
- Show more: a `Spinner` in the button, which is `aria-busy` and disabled. The rows already shown stay.

### 5.2 Empty
- **Whole Overview empty:** `Empty`, inline and left-aligned in the list flow under the picker (§3.2), title **Nothing going on**, description "When the agent starts a goal, sets a watch, schedules something or asks you a question, it shows up here."
- **One lane empty:** the lane is hidden. Lanes never show a placeholder.
- **Section empty:** `Empty`, inline and left-aligned in the list flow under the picker (§3.2), with one line for that type, for example "No watches. The agent sets one when it needs to wait for something." Goals add a **New goal** button (Q15). No other section has an action.
- **Done empty within 24 h, with older items:** inline and left-aligned in the list flow (§3.2), "Nothing finished in the last 24 hours." plus **Show more**.

### 5.3 Error
- **Section failed, never loaded:** `Alert status="warning"`, "Couldn't load {watches}. The agent still has them. Only this view failed." with a **Retry** button. The section never shows Empty when it doesn't know.
- **Lane source failed:** the lane shows one quiet row, "Couldn't check {schedules}", with **Retry**. The other lanes render as normal.
- **Action failed** (pause, clear, restore): an inline `Alert status="warning"` on the item, "Couldn't {pause} it. Nothing changed.", with **Try again**. An error toast is added when the item has left the screen.

### 5.4 Stale
- **A refresh failed after a good load:** keep the last data. Add `Callout tone="neutral"` at the top of the section: "Showing what was loaded at 18:30. Couldn't refresh." with **Retry**.
- **Schedules until #1537 lands:** schedules are polled every 60 s, not pushed. The section shows "Updated 40 s ago" in meta text. It is not an error, and it is removed when the scheduler event ships (Q7).
- **Item changed while its detail view is open** (for example, the agent clears a watch): the detail view stays open. It shows `Callout tone="info"`: "This changed while you were looking. It's now {status}." The open item is never swapped silently.

## 6. Accessibility

- **Frames.** Modal = `Dialog`, phone sheet = `Drawer`: focus trap, Escape, focus return. The rail is a labelled non-modal `region` ("Work"); Escape there only closes an open detail view. In the window, Escape closes the inspector before the dialog.
- **Moving between frames.** Focus moves to **the same element** in the new frame (the open item's heading, or the active section item), and the move is announced (§4.5). Focus never drops to `<body>`.
- **Structure.** Frame heading = view title; lanes and sections are `h2`, groups `h3`. Each lane is a `list`. Counts are in the heading text ("Doing, 3").
- **Rows.** Each row is **one** button opening the detail view (raw `<button type="button">` allowed for composite rows per protoContent#551, with the DS focus ring and tokens only). No nested buttons (fixes audit finding 5). Actions live in the detail view.
- **Section navigation.** `SideNav` gives `aria-current` and keyboard order; the collapsed trigger is a DS `Menu` with radio rows.
- **Confirm and undo.** The inline confirm is a `role="group"` labelled by its prompt; focus moves to the prompt; Keep it or Escape returns focus to the trigger. The undo toast pauses on hover or focus. Undo is a second safety net after the confirm (WCAG 2.2.1).
- **Live regions.** One polite region for routine updates; failures assertive. Bus-driven row updates aren't announced one by one; only new "Needs you" items are.
- **Colour.** Status is a dot **and** a word. Meta text uses `fg-muted`, never `fg-subtle`.
- **Target size.** Actions ≥ `sm` in rail and modal; Expand and Dock are `Button icon` with `aria-label` + `Tooltip`; phone actions `md`, full width (WCAG 2.5.8).
- **Motion.** Inherits reduced-motion handling from `Dialog`, `Drawer`, `SideNav`; no extra frame-move animation under reduced motion.
- **Reflow.** At 390 px and 400 % zoom, rows wrap and nothing scrolls sideways. Technical details values use `overflow-wrap: anywhere`.

## 7. Components

### 7.1 Design-system components (all exist today)

`AppShell`, `SurfaceRail` (`@protolabsai/ui/app-shell`) · `Dialog` (`height="fill"`, `padding="none"`), `Drawer` (`side="bottom"`, `fullScreen`), `Tooltip`, `useToast` (`/overlays`) · `PanelHeader`, `SideNav`, `Accordion`/`AccordionItem` (`/navigation`) · `Menu` (`/menu`) · `Button`, `Badge`, `Count`, `Empty`, `Callout`, `Divider`, `Kbd` (`/primitives`) · `StatusDot`, `Skeleton`/`SkeletonGroup`, `Alert`, `ScrollArea` (`/data`) · `FormField`, `Textarea`, `Switch` (`/forms`) · `CommandPalette` (`/command-palette`).

Decision cards keep their approved design; A1 changes placement only.

### 7.2 Console composites (`apps/web`, not the DS)

| Composite | Purpose |
|---|---|
| `SurfaceHost` | The §3.1 shell host: renders any view in rail, modal or phone sheet; owns Expand and Dock; moves `SurfaceState`; places focus. Plugin views get it automatically with an empty state (Q16). |
| `WorkView` | Sections, lanes, `uiStore` deep-link state |
| `WorkRow` | The shared row (one button, dot, title, meta, badge) |
| `WorkDetail` | Detail pane or pushed view: fields, actions, inline confirm, Technical details |
| `useWorkItems()` | One hook over the eight sources: bus refresh, schedule polling until #1537, server undo |

Tokens only; the build runs `ds_check` over them.

### 7.3 Possible DS contributions (optional, not blocking)

- No new DS component is required.
- `ListGroup` / `ListRow` in `@protolabsai/ui/data` (protoContent#675, optional; adopt once it publishes).
- `SurfaceFrame` in `@protolabsai/ui/app-shell`, only if plugin views need the frame header outside the console host.

### 7.4 Open DS follow-ups

- protoContent#673: `Empty align="start"`, tokenized font sizes.
- protoContent#674: collapsed `SideNav fit="content"`.
- protoContent#675: `ListGroup`/`ListRow`.

None blocks build.

## 8. Out of scope

Decision card design (placement only, A1) · plugin `UtilityWidget`s (Q13) · merging `SubagentsPanel` (link only, Q14) · merging the Background jobs and Activity pills (Q12) · bulk actions · history beyond "Show more" (no search, no date picker) · cross-agent or fleet-wide work · the Q7–Q10 backend changes (dependencies, carded separately) · Expand and Dock on the phone.

## 9. Open questions

All 16 are answered (Josh, 2026-11-09: "All as recommended", and the spec and preview approved with "Looks good to me"). Backend needs Q7 to Q10 are accepted, and the build cards them: the scheduler event (#1537), pause APIs, task and goal edit, and server-side undo (soft delete plus restore, about 8 s).

16. **Plugin views** get Expand and Dock to rail automatically from the host, with an empty `SurfaceState`.

## Build notes

- **B1:** confirm the raw goal and Activity status lists (`types.ts` types them as `string`).
- **B2:** confirm the neutral tone of `StatusDot`/`Badge` resolves to `--pl-color-fg-subtle`.
- **B3:** the approved window uses `Dialog size="wide"` (§3.4).
- **B4:** the approved phone sheet uses `Drawer side="bottom" fullScreen` (§3.5).

## Preview

designSystem artifact `a-1794225393042-a808bf`, "Work view preview (#4955)", v1. Approved by Josh 2026-11-09.
