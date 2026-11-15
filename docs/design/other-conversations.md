# Other conversations — design spec

Status: **FINAL. Revision 4 approved by Josh 2026-11-15** (relayed by Claude Code). Revision 3 shipped in v0.243.0. Revision 4 changes three things: the tab is renamed **From others** (§3.1, §5), the thread footer becomes one row (§3.4, §3.7), and the thread header keeps its structure with slimmer copy (§3.4, §3.7). The tab stays pinned first, as shipped. The transcript and the design system are unchanged; the server only adds the stored origin fields to each list row (§8, C9). §9 lists the two slices to card: C7 and C9.
Surface: protoAgent console (`apps/web`): the chat tab bar (`ChatSurface.tsx`, DS `TabBar`), the session sheet (`SessionSheet.tsx`), and links from Work and Needs you.
Preview: artifact `a-1794767154081-d26a78` v10 (Before and After, desktop 1440 and phone 390, light and dark). Its After desktop column shows the right-pin, which Josh did not approve; read the tab position from §3.1, not from the preview.
Internal names stay: the route `#/conversations/<id>`, the reserved tab id `conversations`, `ConversationsPane.tsx`, `ConversationReader.tsx`, `conversations-store.ts`, `GET /api/conversations`, CSS class names. Only user-facing strings change.

## 1. What this answers

**What is this agent talking about with someone other than me in the console, and is any of it still running?**

Before revision 3, the console listed only console chats. `GET /api/chat/sessions` keeps context ids that match `chat-%` (`src/operator_api/chat_routes.py`). Every A2A thread was stored with its turns, but no screen listed it. On 2026-11-13 and 14 Josh couldn't see protoEngineer working on a friction triage and a release that Claude Code had asked for.

Chats stay chats. Threads that someone else started get one special tab, **From others**, that lists them and reads them, read-only.

**Why a tab, not a section.** Josh, 2026-11-14: "between the work/delegate chips and the proposed dropdown section it would just begin getting way too busy for the chat view." So the design goal is:

> **The chat view gains nothing.** No new row, section, banner, chip, toast or notice above, around or inside a conversation. The only footprint in a chat is the From others tab in the tab bar, with its badge.

§3.8 checks every part of this spec against that goal.

## 2. Decisions

| # | Question | Decision | By |
|---|---|---|---|
| 1 | Scope | Every A2A thread, in two groups: **For you** (open) and **Automated** (collapsed, with a count). Never listed: Activity, background jobs, the command palette, fleet-room, incognito or hidden turns. | Josh, 2026-11-14 |
| 2 | Placement | A special **From others** tab in the chat tab bar, pinned first. It appears only while there are threads in the 7-day window. Selecting it shows the list in the main pane; a thread opens inside the tab, with **← Back**. Phone: a distinct From others item at the top of the session sheet. | Josh, 2026-11-14; renamed 2026-11-15 (§10) |
| 3 | Read-only or interactive | Read-only, with **Continue in a new chat**, which opens a real chat tab. Anything waiting on Josh is answered in Needs you. | Josh, 2026-11-14 |
| 4 | Origin labels | List rows: one muted line, "From Claude Code for Josh · 2 min ago · 13 messages". Status by dot plus word: Running, Needs you, Failed. | Josh, 2026-11-14 |
| 5 | Noise and retention | One row per thread. Needs you, then Running, then newest. Last 7 days, older behind **Show older**. | Josh, 2026-11-14 |
| 6 | Titles | The caller's `title`, else the first line of the first message, else "Conversation from &lt;origin&gt;". The reader hides a repeated origin prefix (§3.4 item 2c). | Josh, 2026-11-14; display rule 2026-11-15 |
| 7 | Links in | Work's provenance line and Needs you items open the thread at `#/conversations/<context_id>`. | Josh, 2026-11-14 |
| 8 | Compact reader | Footer becomes one 48 px row. Header: same structure, slimmer copy. Transcript as shipped. | Josh, 2026-11-15 (§3.4) |
| — | Rename, Delete | Follow-ups, not v1. These apply to threads; the From others tab itself is never renamed or closed (§3.2). | Josh, 2026-11-14 |

## 3. Layout

### 3.1 The From others tab (desktop, ≥ 768 px)

```
[💬 From others (3) ●] │ [● chat ✕] [● chat ✕] [● chat ✕] … [+]
```

- **Position: pinned first**, at the start of the tab bar, before every chat tab, with a hairline divider (`--pl-color-border`) after it. It never moves: chat tabs can be dragged and reordered behind it but never in front of it. This is D1's `TabBarItem.pinned` (@protolabsai/ui 0.74.0), as shipped.
  - ~~Revision 4 draft: pinned at the right, outside the scrolling strip, just left of **+** (needed a new DS option, D2).~~ **Dropped.** Josh, 2026-11-15: "I think I liked it better when conversations (now from others) was on the left."
  - Known limit, unchanged from revision 3: D1's pinned tab sits inside the tab strip's scroll area. With many chats open and the strip scrolled right, it scrolls out of view until the strip is scrolled back. With the strip at rest it is always the first thing visible.
- **When it appears:** as soon as the list has loaded and holds at least one thread in the 7-day window (both groups count). With no threads it is not drawn at all, and the strip looks exactly as it does today. While the list loads it is not drawn, so it never flickers in and out. A deep link (§4.6) always shows it.
- **Label and icon (revision 4):** `MessagesSquare` icon, label **"From others"** (was "Conversations"). *Reason (Josh, 2026-11-15):* every tab is a conversation, so "Conversations" was confusing. What these threads share is that someone else started them, and each row's origin line already reads "From Claude Code for Josh". Chats show a status dot in the icon slot; this tab shows an icon instead, so it reads as a place, not a chat.
- **Badge:** `Count` with the number of threads listed. A small `StatusDot` beside the icon when anything is running (`info`) or needs you (`warning`; wins over running). The tab's dot **never pulses**: motion next to the chat Josh is reading would pull his eye. The accessible name says it in words: **"From others, 3 threads, 1 running, read-only"** (with ", 1 needs you" when it applies).
- **No ✕, no rename, not draggable, no chat context menu.** Double-click does nothing. Right-click opens a short menu: **Hide “From others”** (§3.2).
- **Keyboard:** it is a normal tab in the same tablist, first in arrow-key order.

### 3.2 Hiding it

The tab can't be closed like a chat, because closing a chat means "I'm done with this thread". Instead:

- Right-click → **Hide “From others”**. Hidden per device (local storage, not the S6 chat list).
- **It comes back by itself when a thread needs you.** It also comes back from a deep link, and from the tab-bar background menu (**Show “From others”**), which already hosts New chat.

### 3.3 Selecting the tab: the list

The main pane shows a list view, not a chat:

- **Header:** `h2` "From others", the `Eyebrow` "Read-only", and one muted line: "Threads that other people and agents started with designSystem. You can't type here." The section's accessible name is "From others".
- **For you** group, then the **Automated · N** disclosure (collapsed), then **Show older**. Row anatomy as §3.6.
- **No composer.** The list fills the pane.
- Which chat was selected before is remembered; selecting any chat tab leaves.

### 3.4 Opening a thread: inside the tab

Clicking a row opens the thread in the same pane. The From others tab stays selected.

- **Header band (revision 4: same structure, slimmer copy):** ghost **← Back** button and the title (`h2`, receives focus) on one line, the status `Badge`, then the meta line. No eyebrow. See item 2 below.
- **Turns (unchanged, as shipped):** `Conversation` and `Message`, each labelled with its speaker ("Claude Code for Josh", "designSystem", "protoEngineer").
- **No composer.** Its slot holds the footer row (item 1 below).
- **Waiting on Josh:** a warning `Callout` above the turns: "This conversation is waiting for your answer." with **Answer in Needs you**.
- The tab remembers the open thread: selecting a chat and coming back returns to the same thread. **← Back** returns focus to the row the thread was opened from.

**Revision 4: compact reader.** Josh, 2026-11-15: "The header and footer take up so much room." As shipped at about 1140 px, the header was about 270 px and the footer about 165 px. Measured in the preview at 1440 after revision 4: header 81 px (was 147 px in the preview's rebuild of the shipped reader), footer 48 px (was 106 px), transcript 84% of the pane (was 68%).

Decided:

1. **Footer is one row, 48 px, in the composer's slot.** Left: a lock icon and "Read-only. Replies start a new chat." in muted text. The lock is a focusable `Tooltip` trigger with the full sentence: "You can't type here. Replies go to a new chat, so &lt;origin&gt; isn't interrupted." Right: **Copy link** (ghost, small, link icon) and **Continue in a new chat** (primary, small). *Reason:* the read-only cue stays where a person looks to type, and one row replaces a sentence row plus a button row.
2. **Header: same structure, slimmer copy.** Josh, 2026-11-15: "maybe just slim down the copy we see in the current header." The parts, components and placement stay (Back, title, meta, ghost `Button`, `h2`, `Badge`); the header goes from four lines to two.
   - **a. Drop the eyebrow "Conversation · read-only".** *Reason:* the footer row now says read-only where a person looks to type, the tab is called "From others", and the reader `section` gets `aria-label="Conversation, read-only"` for screen readers. Folding "Read-only" into the meta line would say it a third time.
   - **b. "← Back to list" becomes "← Back", on the title's line.** Accessible name and `Tooltip`: "Back to From others". *Reason:* "Back" is enough next to a selected tab; "← From others" is longer and repeats the tab label beside it. Sharing the title's line removes a whole row.
   - **c. Hide a repeated origin in the displayed title.** If the thread has a stored `caller` (S1), and the title starts with "From &lt;caller&gt;" or "From &lt;on_behalf_of&gt;", optionally followed by up to 60 characters that aren't a colon, then ":" and spaces (for example "From Claude Code (Josh's session): "), the reader drops that prefix and capitalises the first letter. The `h2`'s `Tooltip` keeps the full title, and the list row keeps the full title. *Reason:* the meta line already names the caller, so the prefix spends the first 35 characters of a truncated title saying it twice.
     - Match `caller` and `on_behalf_of` only, case-insensitively, as whole words. Never guess a name.
     - If there is no stored `caller` (an old thread, "another agent"), leave the title as it is: the prefix is then the only place the origin shows.
     - If dropping the prefix leaves nothing but spaces, show the full title.
     - A caller-sent `title` (S1, `delegate_to`'s `summary`) normally has no such prefix, so the rule mostly cleans up titles taken from the first message.
     - Display-only, in the console. The server's `title` is unchanged.
   - **d. Shorter meta line, no leading "From".** "Claude Code for Josh · 14 Nov, 12:29 · 14 messages"; with no metadata, "another agent · 14 Nov, 12:29 · 14 messages". A `Tooltip` keeps the long form ("From Claude Code for Josh · 14 Nov at 12:29 · 14 messages"). *Reason:* the tab already says "From others", so "From" adds nothing, and a comma is shorter than "at". List rows keep "From …" (§3.6), because a row has no header above it.

Dropped or not chosen:

3. ~~One-row header with icon-only ←.~~ **Dropped**, replaced by item 2. Josh wanted the shipped structure with slimmer copy.
4. ~~Speaker label only when the speaker changes; no left inset.~~ **Dropped.** Josh: "just change the footer". The transcript stays as shipped.
5. ~~Option B: everything in one header row and no footer.~~ **Not chosen.**

### 3.5 How a tab avoids "you can type here"

My revision-1 objection was that a tab promises a message box. The tab answers it in six ways:

1. **Fixed first slot with a divider**, apart from the chats.
2. **An icon, not a status dot**, and a label ("From others") that names a group of threads by who started them, not a single chat.
3. **No ✕ and no rename**: the controls every chat tab has are missing.
4. **"Read-only" in text**: in the tab's accessible name, the list header, the reader's accessible name, and the thread footer. (Revision 4 drops the thread header's eyebrow; the footer row says it visibly.)
5. **No composer** anywhere in the tab. In a thread, the composer's slot holds one row: lock icon, "Read-only. Replies start a new chat.", Copy link and **Continue in a new chat**. The full explanation is in the lock's tooltip.
6. **Every message is labelled with its speaker**, and the speakers aren't Josh.

### 3.6 Row anatomy

```
[dot] Title (one line, ellipsis)                     [Badge if not idle]
      From Claude Code for Josh · 12 Nov at 22:54 · 13 messages
```

- One composite `<button type="button">` (protoContent#551 pattern), DS focus ring, tokens only. 44 px minimum.
- Accessible name: "&lt;title&gt;, &lt;origin&gt;, &lt;status&gt;, &lt;relative time&gt;".
- Rows show the full title and the long origin line; revision 4 doesn't change rows.

### 3.7 Phone (< 768 px)

Decided (Josh, 2026-11-14; renamed 2026-11-15): **a distinct From others item pinned at the top of the session sheet**.

- Sheet order: "Chats" title and **New**, the **From others** item, a divider, then the chat rows (unchanged).
- The item uses the same icon, label, count and status dot as the desktop tab, plus "Read-only" in muted text. It has no delete ✕. 44 px minimum. Accessible name as §3.1.
- It appears and hides under the same rules as the desktop tab (§3.1, §3.2). Hiding is a long-press menu with **Hide “From others”**, matching right-click.
- Tapping it closes the sheet and shows the list full screen. The mobile header reads **"From others"** where a chat title would be, on the list and in a thread. Tapping a thread opens it; the header gets **← List** (unchanged). The header's chat-switcher tap still opens the sheet.
- **Thread header (revision 4): same structure, slimmer copy**, as desktop item 2: no eyebrow, the title with any repeated origin removed, then the short meta line. Two lines. Back stays in the app header as **← List**, as shipped. ~~The thread title replaces the app header title, with an ⓘ details popover.~~ **Dropped.**
- **Transcript: unchanged, as shipped.**
- **Thread footer (revision 4): one row**, like desktop. Lock icon and "Read-only" on the left; an icon-only **Copy link** (accessible name "Copy link", `Tooltip`) and **Continue in a new chat** (primary) on the right. 44 px touch targets. *Reason:* the shipped three-row footer (sentence, full-width button, Copy link) took about 165 px of an 844 px screen.

### 3.8 Footprint in the chat view

This table checks the design against the goal in §1. "Chat view" means a chat tab's pane: its messages, its composer and the space around them.

| Item | Where it shows | Verdict |
|---|---|---|
| From others tab, count, status dot | Tab bar, first slot | **Allowed.** The one footprint. Static dot, no pulse (§3.1). |
| Tab appearing for the first time | Tab bar | **Allowed.** The tab simply appears. No banner, toast or chip in the chat. |
| "Needs you" on a thread | The tab's dot turns `warning`; the item is already in the Needs you rail | **Allowed.** Nothing new in the chat. |
| Hidden tab returning when something needs you | Tab bar | **Allowed.** Same footprint as the tab. |
| Hide / Show menus | Right-click on the tab, or on the tab bar background | **Allowed.** Only on request, and in the tab bar. |
| Toast after **Continue in a new chat** | Would float over the new chat | **Removed** (revision 2 had it). The new chat tab opening, selected, with the pre-filled composer is the feedback. |
| Pre-filled composer text in the new chat | The new chat's own composer | **Allowed.** Josh asked for it by pressing Continue. |
| Screen-reader notice when the tab first appears | Live region, not visible | **Allowed, kept narrow.** Once, never repeated. |
| Screen-reader notices about running threads | Live region | **Limited**: only while the From others tab is selected. |
| Links from Work and Needs you | Work view; Needs you rail | **Allowed.** Outside the chat pane. |
| Phone | Session sheet item | **Allowed.** The phone's chat screen gains nothing. |

Nothing else in this spec renders in a chat.

## 4. Details and reasons

### 4.1 Scope

**For you**: `caller_kind` is `person`, or `on_behalf_of` is set (Claude Code for Josh, the DM CLI, another operator). **Automated**: everything else that isn't excluded, including old threads with no metadata, labelled "From another agent". The server never guesses a person.

### 4.2 Origin, time and status

- Origin copy (list rows and tooltips): "From Claude Code for Josh", "From Claude Code", "From brandLaunch", "Delegation from protoEngineer", "From the API", "From another agent". The reader's meta line uses the same words without the leading "From" (§3.4 item 2d).
- Time: local relative in rows, full timestamp in a `Tooltip` (Work §2.3 rules). Message count only above 1.
- Status from `active` and `last_state`. Work tone map: Running `info` pulse, Needs you `warning`, Failed `error`; idle and done show a neutral dot and no badge.
- Live updates from the bus's `turn.started` / `turn.finished` events for `origin: "a2a"` turns (`src/server/a2a.py`). These also make the tab appear the first time a thread starts.

### 4.3 Noise and retention

One row per context id. Within a group: Needs you, Running, then newest. Seven days listed; **Show older** pages back 50 at a time. The list never deletes or hides threads; the task store's existing retention removes them. When the last thread leaves the window, the tab goes away.

### 4.4 Titles

1. `title` from the caller's metadata. `delegate_to` sends its one-line `summary` as `title`.
2. The first line of the first user message, markdown stripped, clipped at 80 characters.
3. "Conversation from &lt;origin&gt;".

No agent-generated titles: a model call per thread, late, and it can disagree with the caller. The reader's prefix rule (§3.4 item 2c) is display-only and applies after this choice.

### 4.5 Continue in a new chat

Creates a `chat-` session, opens and selects it as a normal chat tab, and pre-fills the composer with `About the conversation "<title>" (<origin>): `, plus a hidden reference to the context id so the agent can `recall_session` it. Josh edits and sends. No toast (§3.8). The pre-fill uses the full stored title.

### 4.6 Links in

- Work's provenance line (D8 waiting-task detail, `apps/web/src/work/sections/tasks.tsx`): "Started in a conversation from Claude Code", as a `TextLink` to `#/conversations/<context_id>`.
- Needs you (`apps/web/src/chat/NeedsYouSection.tsx`): an item whose session isn't a `chat-` id gets a ghost **Open conversation** button.
- The route shows the From others tab (even if hidden), selects it and opens that thread, with its group expanded.

### 4.7 The tab is not a session

The From others tab is client-only. Its id is reserved (`conversations`) and never sent to the server, never written to the S6 chat list, never counted as a chat, and never offered by "unused blank chat" logic. The chat store keeps `currentSessionId` pointing at the last chat; a separate stage value (`chat` or `conversations`) says which is showing.

## 5. States and copy

| State | Where | Copy |
|---|---|---|
| No threads | Tab bar | Tab not drawn. |
| Loading (first load) | Tab bar | Tab not drawn until the list arrives. |
| Loading (deep link) | List pane | `SkeletonGroup` of 4 rows under the header. |
| Failed (deep link, or refresh after the tab was shown) | List pane | `Alert` error: "Couldn't load threads from others. Check the connection, then try again." Button: Try again. |
| Automated only | List pane | For you shows "Nothing started for you this week." Automated opens by default. |
| Thread, running | Thread | Badge Running; the last message shows `TypingIndicator`. |
| Thread, needs you | Thread | Callout warning: "This conversation is waiting for your answer." Button: Answer in Needs you. |
| Thread, gone | Thread | `Empty`: "This conversation is no longer stored." Muted: "The agent keeps conversations for a limited time. This one has been cleared." Button: Back to list. |
| Thread, failed | Thread | `Alert` error: "Couldn't load this conversation. Try again." |
| Hidden by Josh | Tab bar | Tab not drawn; it returns when something needs you. Menu items: **Hide “From others”**, **Show “From others”**. |
| Tab first appears | Live region | "From others tab added: 1 thread from Claude Code." (plural: "3 threads") |
| Tab and sheet item | Accessible name | "From others, 3 threads, 1 running, read-only" (", 1 needs you" when it applies). |
| Thread header | Thread | Back button "← Back" (accessible name and tooltip "Back to From others"). Title with a repeated "From &lt;caller&gt;…:" prefix removed (§3.4 item 2c); full title in its tooltip. Meta "&lt;origin without 'From'&gt; · 14 Nov, 12:29 · 14 messages"; long form in its tooltip. Reader accessible name "Conversation, read-only". |
| Thread footer | Thread | Desktop: "Read-only. Replies start a new chat." Phone: "Read-only". Lock tooltip: "You can't type here. Replies go to a new chat, so &lt;origin&gt; isn't interrupted." Buttons: Copy link (icon-only on phone), Continue in a new chat. |

Inside a thread, "conversation" stays the word for one thread ("This conversation is waiting…", "Conversation, read-only"). The rename applies to the tab, the list and the phone header. The gone state's "Back to list" button keeps its wording: it is a dead-end recovery, not the header's Back.

## 6. Component map (@protolabsai/ui)

| Piece | Component |
|---|---|
| From others tab | `TabBar` item with `pinned` (D1, shipped in 0.74.0), `MessagesSquare` icon, `StatusDot`, `badge` count |
| Tab right-click menu | `Menu` / `MenuItem` |
| List header | `Eyebrow`, `h2` |
| Groups | group label `Eyebrow`; Automated is a disclosure |
| Rows | composite `<button>` (protoContent#551), `StatusDot`, `Badge`, `Tooltip` for time |
| Thread header | `Button variant="ghost"`, `h2`, `Badge`, `Tooltip` on the Back button, the title and the meta (revision 4: `Eyebrow` removed) |
| Turns | `Conversation`, `Message`, `Markdown`, `TypingIndicator` (as shipped) |
| Needs you note | `Callout` warning |
| Footer row | lock icon as a focusable `Tooltip` trigger + muted text; `Button` ghost small (Copy link; icon-only with `Tooltip` on phone) + `Button` primary small |
| States | `Empty`, `SkeletonGroup`, `Alert` |
| Phone | an item inside the existing `Drawer side="bottom"` (SessionSheet) |

**No design-system change in revision 4.** Every piece uses components and tokens that exist in @protolabsai/ui today. ~~D2 (`TabBarItem.pinned: "end"`)~~ is dropped with the right-pin.

All colour, spacing and type from `--pl-*` tokens. Meta text uses `--pl-color-fg-muted`; no status token is used as text colour (Work §2.2).

## 7. Accessibility

- The tab is a real `role="tab"` in the chat tablist, first in arrow-key order. Its accessible name includes the count, the status in words and "read-only": "From others, 3 threads, 1 running, read-only".
- The tab's first appearance is announced once, politely: "From others tab added: 1 thread from Claude Code." It isn't announced again when it returns or when the count changes.
- The Automated toggle is a disclosure (`aria-expanded`, `aria-controls`).
- Opening a thread moves focus to its `h2` (`tabindex="-1"`); **← Back** returns focus to the row.
- The reader `section` is named "Conversation, read-only", so dropping the visible eyebrow loses nothing for screen readers.
- The title's tooltip holds the full title; the `h2`'s text is the shortened one. The full title is also the first message in the transcript, so nothing is reachable only by hover.
- The footer's lock is a focusable tooltip trigger with an accessible name that includes the full sentence, so keyboard and screen-reader users reach it; the visible clause already says read-only.
- Read-only is stated in text everywhere (§3.5); the missing composer is never the only signal.
- Running changes are announced politely, at most once every 10 seconds, and only while the From others tab is selected.
- Touch targets 44 px on phone, including the icon-only Copy link.

## 8. Server change

Shipped in revision 3. Four fields stored on a context's **first turn**, sent by the caller in A2A message metadata:

| Field | Meaning |
|---|---|
| `caller` | Display name of whoever sent it. |
| `caller_kind` | `person` or `agent`. |
| `on_behalf_of` | A person's name, optional ("Josh"). |
| `title` | Optional thread title (`delegate_to` sends its `summary`). |

A thread without them is Automated, "From another agent". **Revision 4 server addition (C9):** `GET /api/conversations` rows don't include these fields today (verified on protoEngineer 2026-11-15: a row has `session_id, title, origin_label, group, last_updated, turn_count, active, last_state`). C9 adds `caller`, `caller_kind` and `on_behalf_of` to each row, read from the `conversation_origin` S1 already stores on the thread's first turn; absent fields stay absent, and they remain display-only, never an authorization input. The prefix rule reads them from the row.

## 9. Build slices

Revision 3 slices (D1, S1, S2, C1–C6) shipped in v0.243.0. Revision 4 has two console slices, both in protoAgent, ready to card. ~~D2 and C8 (right-pin)~~ are dropped. Paths were checked on protoLabsAI/protoAgent main on 2026-11-15; files marked (new) don't exist yet.

C7 and C9 touch different files, so they can run in parallel. The one shared area is the reader: C7 does not edit `ConversationReader.tsx` or its test; C9 owns both.

### C7: rename the tab to "From others"

**What:** change every user-facing "Conversations" string for the tab, the list and the phone to "From others". Internal names, the route, the tab id, file names and CSS classes stay.

**Files:**
- `apps/web/src/chat/ChatSurface.tsx`: the pinned tab's `label`, its accessible-name suffix, the first-appearance live announcement, and the Hide / Show menu items.
- `apps/web/src/chat/ConversationsPane.tsx`: the section's `aria-label`, the `h2`, the intro line, and the failed-state message.
- `apps/web/src/app/SessionSheet.tsx`: the sheet item's title, its accessible name, and the long-press Hide item.
- `apps/web/src/app/MobileShell.tsx`: the mobile header text on the list and in a thread.
- `apps/web/src/chat/ChatSurface.test.tsx`: assertions on the old label.
- Any other test or e2e spec under `apps/web` that matches the old strings. The coder greps `apps/web` for "Conversations" (case-sensitive, in strings and JSX text) and lists every file it changed or left.

**Acceptance criteria:**
1. The desktop tab's visible label is "From others", and it stays pinned first with its icon, count and status dot unchanged.
2. The tab's accessible name reads "From others, N threads, M running, read-only", adding ", K needs you" when K > 0, and using "thread" when N is 1.
3. The first-appearance live announcement reads "From others tab added: N thread(s) from &lt;origin&gt;."
4. The tab's right-click menu item reads "Hide “From others”" and the tab-bar background menu item reads "Show “From others”".
5. The list pane's `h2` and accessible name read "From others"; its intro reads "Threads that other people and agents started with &lt;agent name&gt;. You can't type here."; its failed state reads "Couldn't load threads from others. Check the connection, then try again."
6. On phone, the session-sheet item reads "From others" (with its accessible name as in item 2), its long-press item reads "Hide “From others”", and the mobile header reads "From others" on the list and in a thread.
7. Nothing user-visible in the tab, list, sheet or phone header still says "Conversations". Strings inside a thread ("This conversation…") are unchanged.
8. The route `#/conversations/<id>`, the reserved tab id `conversations`, store keys, file names and CSS classes are unchanged; deep links from Work and Needs you still open the tab.
9. Existing unit and e2e tests pass after updating their expected strings; no test is deleted.
10. PR (not a draft; merges on green) with screenshots at 1440 and 390, light and dark.

### C9: compact reader (one-row footer, slimmer header copy)

**What:** §3.4 items 1 and 2 (a–d) and the phone parts of §3.7. The transcript is untouched.

**Files:**
- `apps/web/src/chat/ConversationReader.tsx`: header band and footer row.
- `apps/web/src/chat/conversation-title.ts` (new): a pure `displayTitle(title, { caller, onBehalfOf })` helper for the prefix rule, plus the short-meta formatter if it isn't already shared.
- `apps/web/src/chat/conversation-title.test.ts` (new): unit tests for the helper.
- `apps/web/src/chat/ConversationReader.test.tsx`: update the header and footer assertions.
- `apps/web/src/chat/chat.css`: header and footer layout.
- `apps/web/src/app/mobile-shell.css`: phone footer row.
- `src/operator_api/` conversations list route (the `GET /api/conversations` handler) and its test: add `caller`, `caller_kind`, `on_behalf_of` to each row from the stored `conversation_origin` (§8).
- `apps/web/src/chat/conversations-store.ts` and/or `apps/web/src/lib/api/chat.ts`: carry the three fields on the console row type.

**Acceptance criteria:**
1. **Footer, desktop:** one row of at most 56 px (target 48 px) in the composer's slot: lock icon and "Read-only. Replies start a new chat." on the left; Copy link (ghost, small, link icon) and Continue in a new chat (primary, small) on the right. No separate sentence row.
2. **Footer tooltip:** the lock is focusable and its `Tooltip` reads "You can't type here. Replies go to a new chat, so &lt;origin&gt; isn't interrupted."; its accessible name includes that sentence.
3. **Footer, phone (< 768 px):** one row: lock icon and "Read-only" on the left; an icon-only Copy link (accessible name "Copy link", tooltip) and Continue in a new chat on the right; every target at least 44 px.
4. **Header structure:** desktop shows ← Back and the title on one line, then the meta line; two lines in total. The status `Badge` still shows when the thread isn't idle. There's no eyebrow. The reader `section` is named "Conversation, read-only".
5. **Back:** the button's visible text is "← Back"; its accessible name and tooltip are "Back to From others"; it still returns focus to the row the thread was opened from. On phone, Back stays in the app header as "← List".
6. **Title focus:** opening a thread still moves focus to the `h2`.
7. **Prefix rule (`displayTitle`)**, with unit tests for each case:
   - a. `"From Claude Code (Josh's session): Josh's goal…"` with `caller: "Claude Code"` → `"Josh's goal…"`.
   - b. The same title with `caller: "claude code"` (different case) → prefix removed.
   - c. `"From Josh: please check…"` with `caller: "Claude Code", onBehalfOf: "Josh"` → `"Please check…"` (first letter capitalised).
   - d. With no `caller` (undefined or empty) → title unchanged.
   - e. A prefix naming someone else (`"From brandLaunch: …"` with `caller: "Claude Code"`) → unchanged.
   - f. A partial-word match (`"From Claude Codex: …"` with `caller: "Claude Code"`) → unchanged.
   - g. More than 60 characters before the colon → unchanged.
   - h. A title that is only the prefix (`"From Claude Code: "`) → the full title.
   - i. A title with no "From" prefix → unchanged.
   - j. A `caller` containing regex characters (`"A.B (x)"`) is matched literally.
8. **Title tooltip:** the `h2` shows the result of `displayTitle`; its `Tooltip` shows the full stored title. List rows and the Continue pre-fill still use the full stored title.
9. **Meta line:** reads "&lt;origin without the leading 'From'&gt; · &lt;d Mon&gt;, &lt;HH:mm&gt; · N messages" (for example "Claude Code for Josh · 14 Nov, 12:29 · 14 messages"; "another agent · …" with no metadata); its tooltip holds the long form "From … · 14 Nov at 12:29 · 14 messages". The message count is shown only above 1, as today.
10. **Unchanged:** the transcript (speaker label on every message, bubble layout), the needs-you `Callout`, the running / gone / failed states, and the gone state's "Back to list" button.
11. **Tokens only:** no literal colours, sizes or spacing in the changed CSS; `ds_check` or the repo's token guard passes.
12. At 1440 px the header measures at most about 88 px and the footer at most 56 px; at 390 px nothing overflows horizontally. PR (not a draft; merges on green) with screenshots at 1440 and 390, light and dark.

## 10. Tab decisions

Revision 3 (Josh, 2026-11-14), with revision 4 changes (Josh, 2026-11-15):

1. **Position:** pinned first, before the chats, with a divider after it. (Unchanged.) ~~Revision 4 draft: pinned at the right, outside the scrolling strip.~~ **Dropped**; Josh: "I think I liked it better when conversations (now from others) was on the left."
2. **Hiding:** no ✕. Right-click **Hide “From others”**, per device. It comes back when something needs Josh or a link opens a thread. (Unchanged, renamed.)
3. **Label:** ~~"Conversations"~~ → **"From others"**. Reason: every tab is a conversation; what these share is that someone else started them.
4. **Badge:** the total count plus a status dot for running or needs you. (Unchanged.)
5. **Phone:** a pinned **From others** item at the top of the session sheet. (Unchanged, renamed.)
6. **Reader:** one-row footer on desktop and phone; header with the same structure and slimmer copy (§3.4 item 2); transcript as shipped.

No questions are open.
