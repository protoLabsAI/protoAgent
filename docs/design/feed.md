# Feed: one chronological list

Status: proposal for Josh · preview `a-1794522886074-ebb254` v2
Replaces: the pending section + timeline split in `apps/web/src/activity/ActivitySurface.tsx`. Supersedes the Feed half of `a-1794442794785-cb8a88` (the "Waiting for the agent" section and its Send now / Dismiss buttons are dropped).

## 1. The model

The Feed is one list, newest first. Each thing the agent was asked to do appears once.

- **Queued**: the inbox item hasn't run yet. One quiet row at the top: "<source> · Queued — runs when <agent> is free". No colour, no pill, no button.
- **Ran**: the agent ran it. The same row now shows the result: what the agent said, plus "in response to <stimulus>". It does not move and no second row appears.
- **Failed**: the run ended in `state: "failed"`. One error `Badge` reading "Failed" before the title. The reason is in the detail view.

Nothing in the Feed asks for action. Anything that needs Josh goes to Work › Needs you. "Undelivered page" stops existing as a concept: the backend retries undelivered items when the agent is free (bd-rbk7e).

## 2. What goes away

- The `.inbox-list` pending section, `.inbox-item`, `.inbox-item--stuck`, `.inbox-stuck-flag` and their CSS.
- The NOW / NEXT / LATER priority pills in the Feed (`.inbox-pri` stays in theme.css only if another surface still uses it).
- The dismiss button and the `dismiss()` / `api.deliverInbox` call from the Feed.
- `isPendingNowInboxPage` use in `ActivityWidget`: the pill's "!" alert badge, `activity-badge--alert`, and the "priority-now page needs delivery" label. The pill keeps a plain unread `Count`.
- The oldest-first render and auto-scroll to the bottom. The list is newest first and opens at the top.
- `pendingOnly` on `ActivitySurface` (Work › Activity renders the same merged list, see §6).

## 3. Joining a queued item to its result (backend, needed)

Today an activity entry can't be matched to the inbox item it came from. `src/server/a2a.py:128` sends `inbox_id` in the turn metadata, but `src/activity/store.py` never stores it and `activity.message` never carries it.

Change:

1. `ActivityLog`: add an `inbox_id INTEGER` column through `add_missing_columns`, an `inbox_id` argument on `add()`, and return it from `recent()`.
2. `a2a_impl/executor.py`: pass `_md.get("inbox_id")` into the activity write and into the `activity.message` event.
3. `ActivityEntry` type: add `inbox_id?: number | null`.

Client rule: a pending inbox item whose `id` equals some entry's `inbox_id` is not shown; the entry is. On a live `activity.message` with an `inbox_id`, drop that pending item and insert the entry at the same place. Ordering key for a queued row is the inbox item's `created_at`; for a ran row it stays at the inbox item's position until the next reload, then sorts by its own `created_at` (both are "now" in practice, so it does not jump).

## 4. Rows

One compact row per item, built on the Work row pattern (a raw `<button type="button">` composite row, sanctioned by protoContent#551 option 2):

| Part | Content | Tokens / components |
|---|---|---|
| Dot | `StatusDot`, neutral; `error` when failed | `@protolabsai/ui/data` |
| Title | Queued: the stimulus text. Ran: first line of the response. One line, ellipsis. | `--pl-font-size-sm`; queued title in `--pl-color-fg-muted` |
| Failed marker | `Badge status="error"` "Failed", before the title | `@protolabsai/ui/primitives` |
| Time | relative (`relTime`) | `--pl-font-size-xs`, `--pl-color-fg-subtle` |
| Meta line | Source label · "Queued — runs when <agent> is free" or "in response to “…”" | `--pl-font-size-xs`, `--pl-color-fg-muted`, one line |

Source label uses `triggeredByLabel()` from `work/sections/activity.tsx` plus the trigger: "Scheduled · ds-drift-watch", "Another agent · protoEngineer", "Webhook · github", "Inbox · <source>", "You".

Day headings ("Today", "Yesterday", "Nov 10") group rows. Hover uses `--pl-color-bg-hover`. No borders between rows; padding `--pl-space-2`.

The row's accessible name is the full sentence: "[Failed: ]<title>. <meta>. <time>. Open details".

## 5. Detail

Clicking a row opens the detail in the same dialog body, with a "← Feed" ghost `Button` that returns focus to the row. Fields (a `dl`):

- What triggered it
- When
- In response to (full stimulus)
- Response (markdown) — or, for a failed run, "What went wrong" (the entry's text); for a queued item, "Status: Queued. It runs when <agent> finishes what it's doing now. Nothing for you to do."

The existing open-in-reader action (ADR 0062) moves into the detail view as a ghost icon button.

## 6. Dialog and hosts

- The Feed dialog uses the shared utility size, 800 × min(60dvh, 560px) (#5165); the body scrolls inside it.
- Open question for the build: `ActivityWidget.onOpen` today routes the pill into Work › Activity, not into the utility dialog. Whichever host the pill opens, it renders this one list. Work › Activity drops `<ActivitySurface pendingOnly />` above its rows, so Work also shows each item once.

## 7. States

- **Empty**: `Empty` — "Nothing here yet" / "When <agent> works on its own — a schedule, a message from another agent, a webhook — it shows up here, newest first."
- **Loading**: `Skeleton` rows, `aria-busy`.
- **Couldn't load**: one `Alert status="error"`: "Couldn't load the feed. Check the connection, then reload." If only one of the two fetches fails, show what loaded and the same alert above it.
- **Queued**, **Queued → ran**, **Failed**, **Many**, **Long text**: as in the preview.

## 8. Accessibility

- Rows are buttons in an `ol` per day, under an `h3` day heading.
- Live updates: a polite live region announces "<agent> finished: <title>" when a queued row turns into its result. No announcement for every new row.
- Focus moves to the detail heading on open and back to the row on "← Feed".
- The Failed badge carries the word, not colour alone. Contrast: tokens only, no literal colours.

## 9. Acceptance

1. With a pending inbox item and no matching entry, the Feed shows one row for it at the top, with "Queued — runs when <agent> is free" and no badge, pill or button.
2. When `activity.message` arrives with that item's `inbox_id`, the Feed shows exactly one row for it, now showing the response; no row shows the item twice, on reload too.
3. An entry with `state: "failed"` shows one "Failed" badge; its detail shows the reason.
4. No element in the Feed dismisses, delivers or alerts. `deliverInbox` is not called from the Feed.
5. The pill shows a plain unread count; no alert variant.
6. Work › Activity shows no separate pending block.
7. The list is newest first and does not auto-scroll.
8. Tests: replace the four "pending feed" tests and the blocked-page pill test in `ActivityWidget.test.tsx` with tests for 1–5; keep `OriginProvenance.test.tsx` passing or retire it with the component.
9. Backend: unit test that `ActivityLog.add(inbox_id=…)` round-trips through `recent()`, and the migration adds the column on an existing DB.

## 10. Approval and decisions (added at approval)

Approved by Josh: "feed looks great. ship it".

- §6 resolved: the Feed pill opens the Feed utility dialog (`UTIL_DIALOG_WIDTH`, 800px; util body height, #5165). Work has no Activity section (ADR 0129 Q3: history is not in Work), so the §2 `pendingOnly` bullet, the second §6 bullet and acceptance 6 become: Work has no Activity entry point.
- Depends on bd-rbk7e. The Feed renders no "undelivered page" alert or dismiss-only row.
