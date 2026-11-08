# Directive ledger view — design spec

Status: **APPROVED, ready to build** (Josh, 2026-11-08; decisions recorded under the copy rules). Owner: designSystem. Issue: #4861. Answers to the codebase questions: [directive-ledger-view-answers.md](./directive-ledger-view-answers.md). Interactive preview: published as a claude.ai artifact for Josh's review.

- Surface: core console, chat surface of `apps/web` (issue #4861, follow-up to ADR 0127 D6)
- Requested by: Josh
- Status: draft for Josh's approval. Nothing gets carded until he approves this spec and the preview.
- Uses: `@protolabsai/ui` and `@protolabsai/design` only. No new backend. No new design-system component is required (see "Components").

## 1. What this view answers

This view answers one question at a glance: **what does this agent believe I have allowed it to do in this chat?**

It also gives Josh one way to take something back: withdraw an entry, with a light confirm step.

The ledger is the agent's system of record (ADR 0127 D6). The view shows that record as it is. It never infers, summarizes or rewords the operator's words.

## 2. Information architecture

The view has two levels.

1. **Ledger strip** (always mounted, often zero height). This is a one-line summary above the composer, a sibling of `BackgroundWorkStrip`. It answers "is anything active here?"
2. **Ledger drawer**. This is the full list, opened from the strip's "View" button or from the other entry points in §3.3. It answers "exactly what, under which conditions, and can I withdraw it?"

The drawer has two groups, in this order:

- **Active.** These are the entries the agent re-reads every turn. They are always expanded, oldest first, which matches the API order. Oldest first keeps the order stable when a new entry arrives.
- **History.** These are done and withdrawn entries. The group starts collapsed and lists the newest closure first, because history is read backwards.

Each entry shows these fields, in this order of importance:

| Order | Field | Source | Shown as |
|---|---|---|---|
| 1 | The operator's words | `quote` | A block quote, in the largest text in the drawer. Plain text, line breaks kept, never reworded. |
| 2 | Conditions | `conditions` | "Only if: …". Hidden if empty. |
| 3 | Applies to | `scope` | "Applies to: release v0.231.0". Hidden if empty. |
| 4 | When it was given | `created_at` | "Given today at 17:14" or "Given 7 Nov at 17:14", in local time. The full timestamp is in a tooltip. |
| 5 | How it closed (history only) | `status`, `closed_by`, `closed_at`, `close_reason` | A plain sentence, for example "Withdrawn by you · 7 Nov at 19:02". The reason follows as "Note: …". |
| — | Entry id | `id` | **Never shown.** It is used only for the `DELETE` call and the React key. |

## 3. Placement

### 3.1 The ledger strip

- It sits in the composer dock, **directly above** `BackgroundWorkStrip`. Read from the top down, the dock says "what I'm allowed to do" and then "what I'm doing now". Background work sits closest to the composer because it changes most often.
- It follows the sibling's rules exactly. The container always renders, which gives one stable DOM node and one live region. When nothing is active it has an `--idle` modifier and collapses to zero height. It never reserves an empty line.
- It is **quiet**. There is no tint, no Banner and no warning tone. It uses the same height, type size and ghost `View` button as the background work strip, so the two read as one family. The leading glyph is a neutral `StatusDot` (not a spinner, because nothing is running).
- The strip shows only **active** entries. History alone never expands the strip.

### 3.2 Strip layout

Desktop (1440 px), one line:

```
●  1 active instruction · "once #4801 and #4808 part 1 merge, cut the release, stopping…"   [View]
```

With several entries active, the strip shows only the count and the oldest quote, so it stays one line:

```
●  3 active instructions · "once #4801 and #4808 part 1 merge…" and 2 more   [View]
```

Phone (390 px), one line. The count is shortened and the quote is clipped earlier:

```
●  1 active · "once #4801 and #4808 part…"   [View]
```

The strip clips the quote with an ellipsis. That is allowed because the full text is always one tap away in the drawer. The strip's `title` and `aria-label` also carry the full quotes.

### 3.3 Other ways to open the drawer

The strip is hidden when nothing is active, so the drawer needs another entry point for history and the empty state:

- an item in the chat's overflow menu: **"Standing instructions"** (see open question Q4);
- typing `/directives` in the console opens the drawer, instead of only printing a text reply (see open question Q3).

### 3.4 The drawer

- Desktop: `Drawer side="right"` at the default 420 px width. The chat stays visible and readable behind it.
- Phone: `Drawer side="bottom"` as a bottom sheet, up to the component's default 85dvh cap. The chosen side follows the console's existing mobile breakpoint (see Q9).
- Title: **"Standing instructions"**.
- The body has an intro line, then the Active group, then the History group.
- There is no footer. Each action sits on the entry it acts on.

## 4. Copy

All copy uses plain words. It never shows `directive_id`, `in_force`, `fulfilled`, `revoked` or any other raw value.

> **Decision (Josh, 2026-11-08):** the user-facing label for a standing instruction the agent is still acting on is **"Active"**, not "In force". The API status stays `in_force`; only the copy changes.

> **Decisions (Josh, 2026-11-08):**
> - **Withdraw uses the inline confirm** inside the entry, as specced, not `ConfirmDialog`. (Q12)
> - **A withdrawal adds a note to the chat transcript.** (Q13) Copy: `You withdrew: "{first line of quote}"` plus ` — {reason}` when a reason was given. Requirements:
>   - The server writes it when the DELETE succeeds, so the console panel and any API caller both get it. A `/directives revoke` typed in chat already leaves the operator's own message, so it gets no second note.
>   - It is recorded as an **operator-originated** event, not a system-generated line, so compaction's verbatim operator-message carry (ADR 0127 D3) keeps it next to the original approval. Without it, the original approval survives compaction word for word with nothing after it showing it was withdrawn.
>   - It renders in the transcript as a quiet event row, not a chat bubble, using the transcript's existing system-event style.
>   - This is a small backend addition: the DELETE endpoint appends the event. Card it with the view, and test that a compacted chat carries both the approval and the withdrawal.
> - **The strip stays hidden when only history exists.** (Q14) It shows only while something is active. History stays reachable from the `/directives` palette row and the drawer. Spec approved for build; it ships as a DRAFT PR for Josh's visual review.

### 4.1 Strip

| Case | Text |
|---|---|
| 1 active | **1 active instruction** · "{quote}" |
| N active | **{N} active instructions** · "{oldest quote}" and {N−1} more |
| Phone | **{N} active** · "{quote}" |
| Button | View |
| `aria-label` | "{N} active instruction(s): {quote 1}; {quote 2}…" |
| Could not check (see §5.7) | Couldn't check standing instructions · [Retry] |

### 4.2 Drawer

| Element | Text |
|---|---|
| Title | Standing instructions |
| Intro | What this agent believes you've allowed it to do in this chat. It re-reads everything under "Active" on every turn. |
| Group heading | Active ({N}) |
| Group heading | History ({M}) |
| Conditions label | Only if: |
| Scope label | Applies to: |
| Given | Given today at 17:14 · Given yesterday at 17:14 · Given 7 Nov at 17:14 |
| Entry action | Withdraw… |
| Long-quote toggle | Show full text / Show less |
| Nothing active, history exists | Nothing is active right now. |

### 4.3 How an entry closed (History)

| API state | Text |
|---|---|
| `fulfilled`, by agent | Done · the agent marked it complete {when} |
| `fulfilled`, by operator | Done · you marked it complete {when} |
| `revoked`, by operator | Withdrawn by you · {when} |
| `revoked`, by agent | Withdrawn by the agent · {when} |
| `close_reason` present | Note: {close_reason} |

A small `Badge` before the sentence carries the one-word state: "done" or "withdrawn". Badges render lowercase. They use neutral tone for "done" and neutral tone for "withdrawn". Closing an entry is normal, so neither state uses success or error colour.

### 4.4 Withdraw confirm (inline, inside the entry)

| Element | Text |
|---|---|
| Prompt | Withdraw this instruction? |
| Explanation | The agent will stop treating it as approved from its next turn. Anything it has already scheduled keeps running until you cancel it. |
| Reason field label | Reason (optional) |
| Reason placeholder | For example: holding the release until Monday |
| Confirm button | Withdraw |
| Cancel button | Keep it |

The second sentence of the explanation matters. In the incident, a pending `wait` would have merged on its own. Withdrawing a ledger entry does not cancel scheduled actions, so the copy must not suggest it does (see Q11).

### 4.5 Results

| Case | Text | Where |
|---|---|---|
| Withdrawn | Withdrawn. The agent won't treat it as approved from its next turn. | Toast (neutral), plus the entry moves to History |
| Already closed (409) | This was already closed: {closing sentence from §4.3}. Nothing changed. | Inline `Callout tone="info"` in the entry's place for one view, then the entry moves to History |
| Not found (404) | This instruction is no longer in the list. The list has been refreshed. | Inline `Callout tone="info"`, then refetch |
| Network or server failure | Couldn't withdraw it. It's still active. [Try again] | Inline `Alert status="warning"` inside the open confirm. The typed reason is kept. |

## 5. States

Every state below is in the preview.

### 5.1 Loading

- Strip: it stays idle (zero height) until the first load finishes, so it never flashes "0" or an empty row.
- Drawer: two `Skeleton lines={3}` blocks under the "Active" heading.

### 5.2 Empty: nothing ever recorded

- Strip: idle, zero height.
- Drawer: `Empty`
  - title: **No standing instructions**
  - description: "When you give this agent an instruction or approval that lasts beyond one reply, it records your words here. You can withdraw any of them at any time."
  - no action button.

### 5.3 One active

- Strip: "1 active instruction · "once #4801…"" with the View button.
- Drawer: one entry, fully expanded, with its Withdraw… button. History is shown collapsed only if it has entries.

### 5.4 Several active, plus history

- Strip: "3 active instructions · "{oldest}" and 2 more".
- Drawer: the active entries are listed oldest first, with a `Divider` between them. "History (12)" is a collapsed `AccordionItem`. When opened, it shows closed entries newest first. These entries have no Withdraw button and their quotes use `--pl-color-fg-muted`.
- With 10 or more history entries, the drawer body scrolls (`ScrollArea`). The group headings scroll with the content; they are not sticky. Pagination is not needed at the volumes in the brief.

### 5.5 Long quote

- In the drawer, a quote longer than 8 lines is clamped with a fade to the surface colour. A **Show full text** button sits under it, as a real `Button variant="ghost" size="sm"` with `aria-expanded`. It toggles to **Show less**.
- The clamp is CSS only (`line-clamp`). The whole quote stays in the DOM, so screen readers, find-in-page and copy always get the full text.
- Line breaks in the pasted text are kept (`white-space: pre-wrap`). Long unbroken strings such as URLs wrap (`overflow-wrap: anywhere`) and never cause horizontal scrolling at 390 px.

### 5.6 Withdraw flow

1. **Withdraw…** expands the confirm region inside that entry. It does not open a modal over the drawer, which would mean two stacked overlays on a phone. Focus moves to the "Withdraw this instruction?" prompt, which is the region's label.
2. The reason field is optional. Escape or **Keep it** closes the region and returns focus to the Withdraw… button.
3. **Withdraw** shows `loading` (spinner, `aria-busy`, disabled). **Keep it** is disabled while the request runs.
4. On success, the entry leaves Active and appears at the top of History as "Withdrawn by you · just now". The toast is shown. Focus moves to the next active entry's quote, or to the "Active" heading if none are left. The strip count updates, or the strip collapses.
5. On 409, 404 or a failure, the cases in §4.5 apply.

### 5.7 Failed load

- Drawer: `Alert status="warning"` with title "Couldn't load standing instructions" and body "The agent still has them. Only this view failed to load." It has a **Retry** button (`Button variant="ghost" size="sm"`). The rest of the body is empty. The view never shows "No standing instructions" when it simply doesn't know.
- Strip, when there was never a successful load: one quiet row, "Couldn't check standing instructions", with **Retry** in place of View. It is not tinted. The strip must not collapse, because a collapsed strip would falsely say "nothing active".
- Strip, when a later refresh fails: it keeps the last known content. The drawer shows a small `Callout tone="neutral"`: "Showing what was loaded at 18:30. Couldn't refresh." with Retry.

### 5.8 Already closed (409 race)

The agent can close an entry, for example by calling `fulfil_directive`, between Josh opening the drawer and pressing Withdraw. The server then returns 409. The view refetches, shows the info callout from §4.5 with the real closing sentence, and moves the entry to History. This is shown as information, never as an error.

### 5.9 Themes and sizes

- Light and dark themes use tokens only. There are no literal colours, spacing values or radii. The quote uses `--pl-color-fg`. Meta text and history quotes use `--pl-color-fg-muted`. On `--pl-color-bg-raised` that pairing measures 7.24:1 in dark and 7.73:1 in light, well above the 4.5:1 minimum. Do not use `--pl-color-fg-subtle` for the quote, labels or the closing sentence: it measures 3.42:1 on the light surface and fails.
- At 1440 px: right drawer, 420 px wide.
- At 390 px: bottom sheet. Withdraw and Keep it are `size="md"` and full width, so tap targets are well above the 24 px minimum (WCAG 2.5.8).

## 6. Accessibility

- **Strip.** It is `role="status"`, like its sibling, and its `aria-label` carries the count and the full quotes. A newly recorded entry is announced politely, once.
- **Drawer.** `Drawer` provides the focus trap, Escape to close and focus return to the trigger. The dialog name is the title.
- **Structure.** "Active" and "History" are `h3` headings. Each entry is an `article` labelled by its first quote words. The quote is a `<blockquote>`.
- **Withdraw region.** It is a `role="group"` labelled by its prompt, so a screen reader hears "Withdraw this instruction?, group". The reason `Textarea` is labelled through `FormField`.
- **Results.** Success uses a polite toast, plus moving focus. The 409 and 404 callouts are inserted where the entry was, and the focused element moves to them.
- **Keyboard.** Every control is a DS `Button`, `Textarea` or `AccordionItem`. There is no pointer-only behaviour and no hover-only information. The tooltip on "Given…" repeats information that is already visible.
- **Motion.** It inherits the Drawer and Accordion reduced-motion handling. The entry move to History has no extra animation.
- **Colour.** No state is shown by colour alone. Each badge has a word and each closed entry has a sentence.

## 7. Components

### 7.1 Design-system components used (all exist today)

| Component | Import | Use |
|---|---|---|
| `Button` | `@protolabsai/ui/primitives` | Strip View and Retry (ghost, sm). Withdraw…, Withdraw (danger), Keep it, Show full text. |
| `StatusDot` | `@protolabsai/ui/data` | Neutral dot leading the strip |
| `Drawer` | `@protolabsai/ui/overlays` | The panel: right on desktop, bottom on phone |
| `Accordion`, `AccordionItem` | `@protolabsai/ui/navigation` | The History group |
| `Badge` | `@protolabsai/ui/primitives` | "done" and "withdrawn" on history entries |
| `Count` | `@protolabsai/ui/primitives` | Counts in the group headings |
| `Divider` | `@protolabsai/ui/primitives` | Between entries |
| `FormField`, `Textarea` | `@protolabsai/ui/forms` | Optional reason |
| `Empty` | `@protolabsai/ui/primitives` | Nothing ever recorded |
| `Skeleton` | `@protolabsai/ui/data` | Loading |
| `Alert` | `@protolabsai/ui/data` | Failed load, failed withdraw |
| `Callout` | `@protolabsai/ui/primitives` | 409, 404, stale data |
| `ScrollArea` | `@protolabsai/ui/data` | Drawer body with long history |
| `Tooltip` | `@protolabsai/ui/overlays` | Full timestamp on "Given…" |
| `useToast` | `@protolabsai/ui/overlays` | Withdraw success |

### 7.2 Console composites (in `apps/web`, not in the design system)

| Composite | Purpose |
|---|---|
| `DirectiveStrip` | Sibling of `BackgroundWorkStrip`. It uses the same always-mounted, idle-collapse pattern and reuses its CSS shape as `.chat-ledger` / `.chat-ledger--idle`. |
| `DirectiveDrawer` | The Drawer, the side picked from the breakpoint, the body states. |
| `DirectiveEntry` | One entry: quote, meta, closing sentence, long-quote clamp, inline withdraw region. |
| `useDirectives(sessionId)` | Fetch, refresh, revoke and the 409/404 handling. It exposes the state `loading`, `ready`, `failed` or `stale`. |

The composites use tokens only, for example `--pl-space-*`, `--pl-radius`, the `--pl-color-*` colours and `--pl-font-*` sizes. The build runs `ds_check` over them.

### 7.3 Possible design-system contributions (optional, not blocking)

- **Quote block.** No DS primitive renders a standalone attributed quote; the closest is `pl-markdown`'s blockquote style. If the build finds itself restyling `<blockquote>` in app CSS, that is a sign to contribute a `Quote` primitive to `packages/ui` (props: `children`, `clamp?: number`, `cite?`). Filed only if needed.
- **Composer strip.** `.chat-bgwork` and `.chat-ledger` would be two copies of the same pattern. A shared `ComposerStrip` (icon, label, action, idle collapse, live region) could later move into `@protolabsai/ui/ai`. This is out of scope for #4861.

## 8. Out of scope

- Creating entries from the console. Only the agent records directives (ADR 0127 D6).
- Editing an entry. To change it, withdraw it and tell the agent again.
- Showing directives from other chats.
- Linking an entry to the scheduled actions that act on it (see Q11).

## 9. Open questions

### For Claude Code, against the codebase

1. **Freshness.** When the agent calls `record_directive`, `fulfil_directive` or `revoke_directive` mid-turn, does the console get a stream event? Or must `useDirectives` refetch at turn end or poll? What do other chat-scoped stores, such as `backgroundJobStore`, use?
2. **Mount point.** Where does the chat surface mount `BackgroundWorkStrip`, and which CSS file holds `.chat-bgwork`? Can `DirectiveStrip` mount directly above it in the same dock?
3. **`/directives`.** How does the reserved `/directives` command (#4872) render today? Can the console intercept it to open the drawer, keeping the text reply for non-console clients?
4. **Overflow menu.** Does the chat header have an overflow `Menu` to which a "Standing instructions" item can be added? If not, what is the closest existing chat-level entry point?
5. **`DELETE` reason.** Is `reason` optional on `DELETE …/directives/{id}`? Is there a maximum length, and what is stored as `close_reason` when it is empty?
6. **409 body.** Does the 409 response include the closed entry (`closed_by`, `closed_at`, `close_reason`)? If not, the view refetches to build the closing sentence.
7. **Time formatting.** Is there an existing local-time or relative-time formatter in `apps/web` to reuse for "Given today at 17:14"?
8. **UI store.** Does `uiStore` have a drawer pattern like `openBackgroundJobs`? If so, `openDirectives` should mirror it.
9. **Breakpoint.** Is there an existing mobile breakpoint hook (for example `useIsMobile`) for picking `side="bottom"`, or does `Drawer` handle it?
10. **Session id.** Is the session id available where the strip mounts, the same way `BackgroundWorkStrip` receives `sessionId`?
11. **Linked actions.** Can a pending `wait` or watch be traced to the directive it acts on? If so, the withdraw confirm could list "This will not cancel: {scheduled action}" with a link to cancel it. That would close the incident's actual gap. If not, the copy in §4.4 stands.

### For Josh

12. **Inline confirm or dialog?** This spec uses an inline confirm inside the entry, to avoid a dialog stacked over a drawer on the phone. `ConfirmDialog` is the alternative.
13. **Transcript note.** When you withdraw an entry, should a short system line appear in the chat ("You withdrew: "once #4801…"")? It would make the change visible in the transcript the agent reads, not only in the ledger.
14. **History alone.** Is it right that the strip stays hidden when only history exists, and history is reachable from the overflow menu and `/directives`?
