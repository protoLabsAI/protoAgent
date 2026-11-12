# Work consolidation: four groups

Status: **approved by Josh, 2026-11-12 (v2).** Source: ADR 0129 (Accepted, protoAgent#5100), decisions Q1–Q3, plus Josh's answers to the v1 open questions. §10 (creating work) is new in v3 and is slice 8; slices 1–7 do not wait on it.
Preview: artifact `a-1794444108874-db544b` v2.

> **This spec supersedes the group structure in [work-view.md](./work-view.md)** (its §2.1 Overview lanes and the eight-section list). The row, tone, inspector, confirm and undo rules in work-view.md still apply unless this spec changes them.

## 1. The model

Work has **four groups**, in this order. The same four names are used in Work, in the chat status rail and in the agent's tool map.

| Group | What's in it | Help line |
|---|---|---|
| **Needs you** | Pending decisions; items that need a human (a stuck delivery, a goal that stopped short of its finish line) | Waiting on you. Nothing else in Work asks for your attention. |
| **Doing now** | Running jobs only: running background jobs and the active goal | Running now: background jobs and the active goal. |
| **Coming up** | Not started yet: follow-ups set off by a time (schedule, `wait`) or a condition (watch), and jobs that haven't started (former tasks) | Not started yet: runs at a time, when something happens, or when it's picked up. |
| **Your instructions** | Standing instructions and approvals in force | Your standing instructions and approvals, in force now. |

- **Not in Work:** history (ADR Q3). The activity section is removed from Work. History stays in telemetry, traces and the Activity view. Finished items leave Work; they do not move to a done list.
- **Tasks fold into jobs** (ADR Q1). There is no Tasks section or tag.
- **Not started means Coming up.** A job that hasn't started (an open or deferred former task) shows in **Coming up** as a **Job** with a dashed-circle icon, the meta "not started · added <when>", and the actions **Start now** and **Close job**. It moves to Doing now when it starts. It is **never** counted in Doing now, in Work or in the rail.
- A blocked job that has started stays in Doing now with the word "blocked" in its meta.

## 2. One row, kind as a tag

Every item uses the third-pass `WorkRow` (icon, title, meta, `Badge`). The badge names the kind.

| Group | Tags | Icon |
|---|---|---|
| Needs you | Decision · Delivery · Goal stopped | `StatusDot` info / warning / neutral |
| Doing now | Goal · Job | target · `Spinner` |
| Coming up | Time · Condition · Job | clock · eye · dashed circle (not started) |
| Your instructions | Instruction · Approval | `StatusDot` info |

- A goal is a job with a finish line (ADR D3). Row meta: "step 3 of 8 · checking after each turn". Details show the finish line and progress.
- A goal that stops without meeting its finish line leaves Doing now and appears in **Needs you** as "Goal stopped", with **Keep going** and **Drop the goal**. Neutral tone, never error.
- A goal that meets its finish line leaves Work (it is history).

## 3. Your instructions (ADR D4)

- **Headline = what is approved** (the directive's `scope`), not the quote. If the scope is empty, fall back to the quote.
- **Provenance below, muted, in "you" wording:** `You said “Don't merge anything into protoAgent main until I say so.” in this chat · today 09:12`. Rows keep "You said…".
- **Linked to a decision → Approval.** Row meta: `You chose “<option label>” · 16:25`. Details show **You were asked** (the question), **You chose** (the option and its answer time), the provenance line with **Your words**, and **Open decision**.
- **Tied to a job (Q2):** meta ends with "ends with <job>"; details say "Lasts: until “<job>” finishes. Then it closes itself." and offer **Open the job**.
- **When the job ends,** the tied item is recorded as fulfilled and leaves the group in the same update that removes the job. Both counts drop together. The rail's live region says once: "<job> finished. 1 approval it carried is now fulfilled." There is no fulfilled list in Work.
- Not tied to a job: stays until fulfilled or withdrawn. **Withdraw** is in the details.

### 3.1 Decision link rule (interim)

Until ADR 0129 D4's server-recorded approvals land, the console links a directive to a decision with this rule:

- Same session, and the decision is `answered`.
- `0 ≤ directive.created_at − decision.answered_at ≤ 120 s` (the answer came within 2 minutes **before** the directive was recorded).
- If several decisions match, take the one answered closest to the directive.
- No match: a plain **Instruction** row with "You said…". Never guess beyond the window.

The rule lives in one pure function, `linkDecision(directive, decisions)`, with unit tests for: inside the window, exactly 120 s, 121 s, answer after the directive (no link), another session (no link), two candidates (closest wins). When the server field lands, `linkDecision` reads it and the heuristic is deleted. Dependency: answer timing is reliable only once bd-393xp (protoAgent#5099) records `answered_at` when the interrupt is consumed, not when the resumed turn ends.

## 4. Layout (unchanged from the third pass)

Rail, window and phone sheet keep the approved third-pass layout: the list is the page; detail pushes (rail, phone) or opens an inspector (window ≥ 48rem); one bordered `ul` per group; `h2` heading with its count; title and meta side by side at 40rem or wider.

- **Picker:** Overview plus the four groups, each with its count ("Doing now · 3"). Sized to its label.
- **Overview:** shows only groups with items. A picked group shows its help line and, if empty, its inline empty text ("Nothing planned.").
- **All empty:** one inline `Empty`: "Nothing to track. Nothing needs you, nothing is running or planned, and you haven't given standing instructions." (Slice 8 adds a line and a **New** button; see §10.1.)
- **Scope:** Work covers all chats. A row from another chat names it in the meta ("chat: Work second pass"). Details show a **This chat** badge for the current chat's items.
- **Phone:** rows ≥ 48px; actions `md`, full width, stacked.

## 5. Chat status rail: same groups, same counts

| Chip | Desktop | Phone | Count covers |
|---|---|---|---|
| Needs you (primary `Button xs`, first) | Needs you 2 | 2 | **fleet-wide** |
| Doing now | Doing now · goal 3/8 · 3 | 3 | this chat, running only |
| Coming up | Coming up 3 | 3 | this chat |
| Your instructions | **Told it 3** | 3 | this chat |
| People | unchanged | unchanged | — |

- **Count rule:** a chip's count equals the rows the matching Work group shows with `thisChat` applied. Needs you is never filtered. One shared selector (`workGroups`) computes both, so they can't drift. Not-started jobs are in Coming up, so they never reach the Doing now chip.
- The goal stays visible inside the Doing now chip ("goal 3/8"). With no goal, the chip uses the spinner icon.
- Zero-count chips don't render. The empty-rail rule, the `+K` people overflow and the decision marker's room rules are unchanged.
- **Details panel:** one section per group, using the Work group names ("Your instructions · 3", not "Told it"), each with **Open in Work** (deep link to that group, `thisChat`). The Needs you section keeps the approved decision card behaviour (full card, answer states, stale hint).
- **Spoken labels:** "Doing now, 3 running in this chat, including the goal at step 3 of 8. Show details"; "Told it: 3 instructions in force in this chat. Show details"; "2 things need you. Show them".

## 6. Accessibility

- Group headings are `h2` with the count in the accessible name ("Doing now, 3").
- Each row is one `button`; the open row has `aria-current="true"`.
- The rail keeps `role="group"`, one polite live region, `aria-haspopup="dialog"` and `aria-expanded` on each chip, focus trap and return in the popover; Escape closes the inspector before the dialog.
- Tone: only Needs you uses the accent fill. Delivery problems use warning; nothing uses error.
- At 390px and 400% zoom nothing scrolls sideways.

## 7. Code scope (protoAgent)

1. `groupFor(item)` in `tone.ts` replaces `laneFor`; a shared `workGroups` selector feeds `WorkOverview` and the chat status rail. Client-side mapping (ADR S2): decisions + undelivered pages + stopped goal → Needs you; running background jobs + active goal + started tasks → Doing now; schedules + watches + waits + not-started tasks → Coming up; directives → Your instructions.
2. Work: four group sections replace the eight; the activity section is deleted; `registerWorkSection` plugins must name a group.
3. Your instructions rows with `linkDecision` (§3.1).
4. Chat status rail chips per §5.
5. Tied directives hide on job end client-side now; server fulfil-on-job-end lands with ADR S3.

## 8. Design-system needs

None new for §1–§9. §10 uses `Dialog` (rail, window), bottom `Drawer` (phone), `Textarea`, `Input`, `Select`, `Chip`, `Callout`, `Badge`, `Spinner`, `Button`. Open DS gaps G1 (`Empty align="start"`, protoContent#673) and G3 (list group primitive) still apply.

## 9. Changes from v1

1. Headings use "you" wording: Needs you · Doing now · Coming up · Your instructions.
2. Rail chip for instructions reads "Told it N".
3. Not-started jobs move from Doing now to Coming up and don't count in either Doing now.
4. Decision link: answer within 2 minutes before the directive was recorded (interim, until ADR 0129 D4).

## 10. Creating work from Work (v3, slice 8)

The operator can add work from Work, not only read and act on it. Everything created is one of ADR 0129's three primitives: a **job**, a **follow-up** (time or condition trigger), or an **instruction**. No new kind.

### 10.1 Entry points

- **New** (`Button ghost sm`, plus icon, label "New") at the end of Work's header, on every surface, every state.
- **"+"** (`Button ghost xs icon`, accessible name "New in <group>") in the heading of **Doing now**, **Coming up** and **Your instructions**. It opens the same dialog, only with a group-specific placeholder. It does **not** fix the kind: the agent's reading still decides. **Needs you has no "+"**; only the agent puts things there.
- **All-empty state** adds: "Tell the agent what to do, and when: now, later, when something happens, or as a standing rule." and a primary **New** button.
- Not in the chat rail. The composer is already the way to ask from chat.

### 10.2 Step 1: one question

- `Dialog` (rail and window), bottom `Drawer side="bottom"` at phone width. Heading "New".
- **Phone width means the viewport, not the surface name.** At any viewport ≤ 40rem (a 390 phone), the New flow (step 1, step 2 and Edit) is the bottom sheet on every Work surface, including the rail. It is never a centered `Dialog` there.
- **Sheet layout rules (no horizontal overflow):**
  - The sheet is full width: `width:100%`, no side border, top corners rounded, at the bottom of the viewport.
  - The sheet body is a one-column grid, `grid-template-columns:minmax(0,1fr)`, and every child has `min-width:0`. The same goes for the step-2 field grid (`minmax(0,1fr)` tracks, `min-width:0` on each field) and the footer flex row. A `1fr` track without `minmax(0, …)` grows to its widest nowrap child; that was the bug.
  - `Textarea`, `Input` and `Select`: `width:100%; max-width:100%; box-sizing:border-box`.
  - Example chips: `max-width:100%`, and the label wraps (`white-space:normal; overflow-wrap:anywhere`). Chip has no wrap option today, so 8c applies this as a local override on the examples group and files the DS gap (a `wrap` prop on `Chip`). Truncating with an ellipsis is the fallback if wrapping is refused, but then the chip needs `title` set to the full text.
  - Footer buttons: full width, stacked, `md` size, at least 44px tall. Primary on top, Cancel below.
- One labelled `Textarea`: **"What should the agent do, and when?"** Hint: "Say it in your own words. You'll see how the agent reads it before anything is saved."
- Example chips under the box fill it (they don't pick a kind): "Check #874 every 30 min and rebase it when #868 merges" · "Every weekday at 9, summarise what merged overnight" · "Merge only when the review says PASS" · "Re-run the design-system audit on protoContent".
- **Continue** (primary, disabled while empty) and **Cancel**.
- While the agent reads it: the box is read-only and a status line says "Reading what you wrote…" with a `Spinner`. Nothing is saved.

### 10.3 Step 2: confirm

- Heading "Check this before saving".
- **The agent's reading, in plain words, first**, in an info `Callout`: "I'll check #874 every 30 min and rebase it when #868 merges."
- "Goes in **Coming up** as `Condition`" — the group and tag, so the operator sees where it lands.
- **Editable fields**, filled in by the agent:

| Reading | Group · tag | Fields | Primary button |
|---|---|---|---|
| Now | Doing now · Job | What · Who does it (agent in this chat, or a delegate) · Starts: now (read-only) | **Start** |
| At a time / later / every … | Coming up · Time | First run · Repeat (Once, Every 30 min, hourly, daily, weekdays, weekly) · Then | **Save** |
| When something happens | Coming up · Condition | When · Check with (a registered plugin verifier) · Check every · Then · Runs (once or each time) · Stop checking after | **Save** |
| A standing rule | Your instructions · Instruction | **Your words** (read-only quote) · What it covers (scope) · Lasts · Recorded in: this chat | **Save** |

- **The quote is the operator's typed text, byte for byte.** The agent never rewrites it; it only proposes `scope` and how long it lasts. So there is no paraphrase problem. Hint under the quote: "Saved exactly as you typed it. To change your words, withdraw this and add a new one."
- Repeat is a plain choice, never a cron string. A custom repeat the list can't express shows as the agent's sentence with **Change the words** to rephrase.
- **← Change the words** goes back to step 1 with the text kept. **Cancel** closes; nothing was saved.
- If the agent can't map the words to a registered verifier or a time, the callout says what it couldn't do ("I can't watch Slack messages yet. I can check a PR, a check run or a board card.") and Save stays disabled until the fields are complete.

### 10.4 After save

- The dialog closes. The new item appears in its group with its details open (inspector or push). The live region says once: "Saved to <group>."
- Provenance: Added "Just now, by you, from Work"; an instruction reads "You said “…” in Work, for this chat".
- Everything is created in **the current chat** (`session_id` = the chat Work was opened from), so a follow-up resumes there and the rail counts it.

### 10.5 Edit and cancel from the inspector

- Every **Time** and **Condition** follow-up gets **Edit** first in its actions, then **Run now** (time) and **Cancel**. Every **Instruction** and **Approval** gets **Edit**, then **Withdraw**.
- **Edit** opens the step-2 form, filled with the current values. No agent turn. Heading "Edit follow-up" or "Edit instruction"; button **Save changes**.
- Editing a condition **keeps its history and its checks so far** (it patches the watch in place; it never clears and re-creates). This fixes the merge-watch-874 case, where a stale "Then" had to be fixed by hand through the API.
- An instruction's quote stays read-only; scope and how long it lasts are editable.
- Cancel and Withdraw use the existing confirm and undo rules from work-view.md.

### 10.6 What the console needs from the backend

The agent interprets; the console saves. Two calls, no new primitive.

1. **Interpret = a normal chat turn into the current chat** with a structured reply. The console sends the text with a marker the server routes to a read-only interpret mode (the agent may read tools and the chat's context, so "#874" means what it means here, but must **not** create, schedule or record anything in this turn). The turn ends with one structured reply:

   ```json
   {
     "says": "I'll check #874 every 30 min and rebase it when #868 merges.",
     "kind": "job | followup | instruction",
     "group": "doing | coming | told",
     "job":         { "what": "…", "who": "agent | <delegate>" },
     "followup":    { "trigger": "time | condition",
                      "time":      { "first_run": "ISO", "repeat": "once | <cron>", "repeat_label": "Every weekday", "timezone": "IANA" },
                      "condition": { "condition": "PR #868 merges", "check": "<plugin verifier>", "check_args": {}, "interval_s": 1800, "repeat": false, "expires_in_s": 604800 },
                      "then": "Rebase #874 onto main" },
     "instruction": { "quote": "<the typed text, unchanged>", "scope": "Merge only on a PASS review", "conditions": "" },
     "problem": null
   }
   ```

   The console overwrites `instruction.quote` with the typed text before showing it, so a model slip can't change the operator's words. The interpret turn is not shown as a chat message in the transcript (it is tagged and folded, like other system turns).

2. **Save = the existing operator APIs, called directly by the console** with the confirmed fields:
   - Time follow-up: `POST /api/scheduler/jobs` (context = this chat); edit `PUT /api/scheduler/jobs/{id}`; cancel `DELETE`.
   - Condition follow-up: `POST /api/watches` (run_session = this chat); edit `PATCH /api/watches/{id}`; cancel `DELETE`.
   - Job now: sends "Start: <what>" as a normal turn into this chat (or a background delegation when "Who" is a delegate). Not-started jobs keep the existing `/api/tasks/issues` store; the console no longer offers a "task" anywhere.
   - Instruction: **needs a new operator route**: `POST /api/directives` (quote, scope, conditions, session_id; author = operator), `PATCH /api/directives/{id}` (scope, conditions only; quote immutable), `POST /api/directives/{id}/withdraw`. Today directives are recorded only by the `record_directive` tool and the `/directives` command; there is no REST route in `src/operator_api/routes.py`.

### 10.7 Accessibility

- The dialog is `role="dialog"` `aria-modal="true"`, labelled by its heading; focus goes to the text box (step 1) or the first field (step 2); Tab is trapped; Escape closes it and returns focus to the button that opened it.
- The "Reading…" line is `role="status"`. The agent's reading is plain text, not only colour.
- Every field has a visible label. The read-only quote is a `blockquote` labelled "Your words".
- Phone 390: bottom sheet (see §10.2 sheet layout rules), full-width stacked actions ≥ 44px. The primary button sits on top and Cancel below it; visual order and reading order match. Nothing scrolls sideways at 390 or 400% zoom (WCAG 1.4.10 Reflow), inside the sheet as well as on the page.

### 10.8 Code scope (slice 8)

Slice 8 depends on the slice that lands the four groups and the inspector (§7.2). It is ≤ 4 files per card, so it splits into:

- **8a (server):** directives operator routes (§10.6 2), with tests.
- **8b (server):** interpret mode for a marked chat turn and its structured reply (§10.6 1), with a test that interpret mode creates nothing.
- **8c (console):** New button, group "+", empty-state line, step-1 dialog. Depends on 8b.
- **8d (console):** confirm step, save calls, Edit from the inspector. Depends on 8a and 8c.

**Acceptance criterion for both 8c and 8d (required):** no horizontal overflow at 390 px in the New sheet, with the longest example chip. Concretely, at a 390 × 844 viewport, on the rail and window Work surfaces:

1. The New flow renders as the bottom sheet, full width, at the bottom of the viewport. It is not a centered `Dialog`.
2. With the longest example chip clicked into the box ("Check #874 every 30 min and rebase it when #868 merges"), and with an unbroken URL-length text typed in, no descendant of the sheet extends past the sheet's left or right edge, and the sheet's `scrollWidth` equals its `clientWidth`.
3. The textarea fits inside the sheet's content box. Every example chip shows its full text, wrapped, with no clipping (`scrollWidth ≤ clientWidth`).
4. Footer buttons are full width, stacked, at least 44px tall, primary on top.
5. 8d also covers step 2 and Edit for all four readings (job, time, condition, instruction).
6. The PR attaches a 390 screenshot of the New sheet with the longest chip filled in (8c), and of the condition confirm step (8d). An e2e test asserts points 1–4 at 390.

Bug that prompted this (Josh, v3 preview): at 390 the New dialog stayed a centered dialog, and a nowrap example chip widened its `1fr` grid track, so the intro line, textarea, chip and Continue button were all clipped.
