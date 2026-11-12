# Work consolidation: four groups

Status: **approved by Josh, 2026-11-12 (v2).** Source: ADR 0129 (Accepted, protoAgent#5100), decisions Q1–Q3, plus Josh's answers to the v1 open questions.
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
- **All empty:** one inline `Empty`: "Nothing to track. Nothing needs you, nothing is running or planned, and you haven't given standing instructions."
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

None new. Uses `SideNav`/select, `Count`/`Badge`, `StatusDot`, `Spinner`, `Button`, `Chip`, `Popover`, `Drawer`, `Dialog`, `Empty` and tokens. Open DS gaps G1 (`Empty align="start"`, protoContent#673) and G3 (list group primitive) still apply.

## 9. Changes from v1

1. Headings use "you" wording: Needs you · Doing now · Coming up · Your instructions.
2. Rail chip for instructions reads "Told it N".
3. Not-started jobs move from Doing now to Coming up and don't count in either Doing now.
4. Decision link: answer within 2 minutes before the directive was recorded (interim, until ADR 0129 D4).
