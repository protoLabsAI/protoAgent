# Other conversations — design spec

Status: **FINAL, revision 3**, 2026-11-14. Josh approved revision 3 for build on 2026-11-14, with all five tab choices in §10. Josh changed placement from a section under the tabs (revision 2) to a special **Conversations tab**, so the chat view gains nothing (§1, §3.8). Everything else in §2 stays decided. §9 is final and ready to card.
Surface: protoAgent console (`apps/web`): the chat tab bar (`ChatSurface.tsx`, DS `TabBar`), the session sheet (`SessionSheet.tsx`), and links from Work and Needs you.
Preview: artifact `a-1794687414432-e117ed` v4, desktop 1440 and phone 390, light and dark.

## 1. What this answers

**What is this agent talking about with someone other than me in the console, and is any of it still running?**

Today the console lists only console chats. `GET /api/chat/sessions` keeps context ids that match `chat-%` (`src/operator_api/chat_routes.py`, the `_newest_turn_ranked` filter). Every A2A thread is stored with its turns, but no screen lists it. On 2026-11-13 and 14 Josh couldn't see protoEngineer working on a friction triage and a release that Claude Code had asked for.

Chats stay chats. Outside conversations get one special tab that lists them and reads them, read-only.

**Why a tab, not a section.** Josh, 2026-11-14: "between the work/delegate chips and the proposed dropdown section it would just begin getting way too busy for the chat view." So the design goal for revision 3 is:

> **The chat view gains nothing.** No new row, section, banner, chip, toast or notice above, around or inside a conversation. The only footprint in a chat is the Conversations tab in the tab bar, with its badge.

§3.8 checks every part of this spec against that goal.

## 2. Decisions

| # | Question | Decision | By |
|---|---|---|---|
| 1 | Scope | Every A2A thread, in two groups: **For you** (open) and **Automated** (collapsed, with a count). Never listed: Activity, background jobs, the command palette, fleet-room, incognito or hidden turns. | Josh, 2026-11-14 |
| 2 | Placement | A special **Conversations** tab in the chat tab bar. It appears only while there are outside conversations in the 7-day window. Selecting it shows the list in the main pane; a thread opens inside the tab, with **Back to list**. Phone: a distinct Conversations item in the session sheet. | Josh, 2026-11-14 (replaces revision 2's section; tab details in §10) |
| 3 | Read-only or interactive | Read-only, with **Continue in a new chat**, which opens a real chat tab. Anything waiting on Josh is answered in Needs you. | Josh, 2026-11-14 |
| 4 | Origin labels | One muted line: "From Claude Code for Josh · 2 min ago · 13 messages". Status by dot plus word: Running, Needs you, Failed. | Josh, 2026-11-14 |
| 5 | Noise and retention | One row per thread. Needs you, then Running, then newest. Last 7 days, older behind **Show older**. | Josh, 2026-11-14 |
| 6 | Titles | The caller's `title`, else the first line of the first message, else "Conversation from &lt;origin&gt;". | Josh, 2026-11-14 |
| 7 | Links in | Work's provenance line and Needs you items open the thread at `#/conversations/<context_id>`. | Josh, 2026-11-14 |
| — | Rename, Delete | Follow-ups, not v1. These apply to threads; the Conversations tab itself is never renamed or closed (§3.2). | Josh, 2026-11-14 |

## 3. Layout

### 3.1 The Conversations tab (desktop, ≥ 768 px)

```
[💬 Conversations (7) ●] │ [chat tab ✕] [chat tab ✕] [+]
```

- **Position: pinned first**, at the start of the tab bar, before every chat tab, with a hairline divider (`--pl-color-border`) after it. It never moves: chat tabs can be dragged and reordered behind it but never in front of it. Why first rather than at the end: the end of the strip moves every time a chat opens or closes, and it is the part that overflows into the responsive menu first. A fixed first slot is always in the same place.
- **When it appears:** as soon as the list has loaded and holds at least one thread in the 7-day window (both groups count). With no outside conversations it is not drawn at all, and the strip looks exactly as it does today. While the list loads it is not drawn, so it never flickers in and out. A deep link (§4.6) always shows it.
- **Label and icon:** `MessagesSquare` icon, label "Conversations". Chats show a status dot in the icon slot; this tab shows an icon instead, so it reads as a place, not a chat.
- **Badge:** `Count` with the number of threads listed. A small `StatusDot` beside the icon when anything is running (`info`) or needs you (`warning`; wins over running). The tab's dot **never pulses**: the tab sits next to the chat Josh is reading, and motion there would pull his eye. The pulse is kept for the Running rows inside the list. The accessible name says it in words: "Conversations, 7 conversations, 1 running, read-only".
- **No ✕, no rename, not draggable, no chat context menu.** Double-click does nothing. Right-click opens a short menu: **Hide Conversations tab** (§3.2).
- Keyboard: it is a normal tab in the same tablist, first in arrow-key order.

### 3.2 Hiding it

The tab can't be closed like a chat, because closing a chat means "I'm done with this thread". Instead:

- Right-click → **Hide Conversations tab**. Hidden per device (local storage, not the S6 chat list).
- **It comes back by itself when a conversation needs you.** It also comes back from a deep link, and from the tab-bar background menu (**Show Conversations tab**), which already hosts New chat.

### 3.3 Selecting the tab: the list

The main pane shows a list view, not a chat:

- **Header:** `h2` "Conversations", the `Eyebrow` "Read-only", and one muted line: "Conversations other people and agents had with designSystem. You can't type here."
- **For you** group, then the **Automated · N** disclosure (collapsed), then **Show older**. Row anatomy as §3.6.
- **No composer.** The list fills the pane.
- Which chat was selected before is remembered; selecting any chat tab leaves.

### 3.4 Opening a thread: inside the tab

Clicking a row opens the thread in the same pane. The Conversations tab stays selected.

- **Header band:** ghost **← Back to list** button, `Eyebrow` "Conversation · read-only", the title (`h2`, receives focus), the status `Badge`, the origin line.
- **Turns** with `Conversation` and `Message`, each labelled with its speaker ("Claude Code for Josh", "designSystem", "protoEngineer").
- **No composer.** Its slot holds a footer bar: "You can't type here. Replies go to a new chat, so Claude Code isn't interrupted." with **Continue in a new chat** (primary) and **Copy link** (ghost).
- **Waiting on Josh:** a warning `Callout` above the turns: "This conversation is waiting for your answer." with **Answer in Needs you**.
- The tab remembers the open thread: selecting a chat and coming back returns to the same thread. **Back to list** returns focus to the row it came from.

### 3.5 How a tab avoids "you can type here"

My revision-1 objection was that a tab promises a message box. The tab answers it in six ways:

1. **Fixed first slot with a divider**, apart from the chats.
2. **An icon, not a status dot**, and a plural label ("Conversations") that names a list, not a thread.
3. **No ✕ and no rename**: the controls every chat tab has are missing.
4. **"Read-only" in text**: in the tab's accessible name, the list header and the thread header.
5. **No composer** anywhere in the tab. In a thread, the composer's slot explains why and offers **Continue in a new chat**.
6. **Every message is labelled with its speaker**, and the speakers aren't Josh.

### 3.6 Row anatomy

```
[dot] Title (one line, ellipsis)                     [Badge if not idle]
      From Claude Code for Josh · 12 Nov at 22:54 · 13 messages
```

- One composite `<button type="button">` (protoContent#551 pattern), DS focus ring, tokens only. 44 px minimum.
- Accessible name: "&lt;title&gt;, &lt;origin&gt;, &lt;status&gt;, &lt;relative time&gt;".

### 3.7 Phone (< 768 px)

Decided (Josh, 2026-11-14): **a distinct Conversations item pinned at the top of the session sheet**, the phone version of the pinned-first tab.

- Sheet order: "Chats" title and **New**, the **Conversations** item, a divider, then the chat rows (unchanged).
- The item uses the same icon, label, count and status dot as the desktop tab, plus "Read-only" in muted text. It has no delete ✕. 44 px minimum.
- It appears and hides under the same rules as the desktop tab (§3.1, §3.2). Hiding is a long-press menu, matching right-click.
- Tapping it closes the sheet and shows the list full screen. The mobile header reads "Conversations" where a chat title would be. Tapping a thread opens it; the header gets **← List**. The header's chat-switcher tap still opens the sheet.
- The thread footer stacks: the note, then the two buttons full width.

### 3.8 Footprint in the chat view

This table checks revision 3 against the goal in §1. "Chat view" means a chat tab's pane: its messages, its composer and the space around them.

| Item | Where it shows | Verdict |
|---|---|---|
| Conversations tab, count, status dot | Tab bar | **Allowed.** The one footprint. Static dot, no pulse (§3.1). |
| Tab appearing for the first time | Tab bar | **Allowed.** The tab simply appears. No "new outside conversation" banner, toast or chip in the chat. |
| "Needs you" on a thread | The tab's dot turns `warning`; the item is already in the Needs you rail | **Allowed.** Nothing new in the chat. The Needs you rail exists today, outside the chat pane. |
| Hidden tab returning when something needs you | Tab bar | **Allowed.** Same footprint as the tab. |
| Hide / Show menus | Right-click on the tab, or on the tab bar background | **Allowed.** Only on request, and in the tab bar. |
| Toast after **Continue in a new chat** | Would float over the new chat | **Removed** (revision 2 had it). The new chat tab opening, selected, with the pre-filled composer is the feedback. |
| Pre-filled composer text in the new chat | The new chat's own composer | **Allowed.** Josh asked for it by pressing Continue; it's ordinary composer text he can edit or clear. No extra chip or quote block. |
| Screen-reader notice when the tab first appears | Live region, not visible | **Allowed, kept narrow.** A sighted person sees the tab appear; a screen-reader user needs the same fact once. Never repeated. |
| Screen-reader notices about running threads | Live region | **Limited**: only while the Conversations tab is selected. Never while a chat is selected. |
| Links from Work and Needs you | Work view; Needs you rail | **Allowed.** Outside the chat pane. |
| Phone | Session sheet item | **Allowed.** The sheet is the phone's tab bar. The phone's chat screen gains nothing. |

Nothing else in this spec renders in a chat.

## 4. Details and reasons

### 4.1 Scope

**For you**: `caller_kind` is `person`, or `on_behalf_of` is set (Claude Code for Josh, the DM CLI, another operator). **Automated**: everything else that isn't excluded, including old threads with no metadata, labelled "From another agent". The server never guesses a person.

### 4.2 Origin, time and status

- Origin copy: "From Claude Code for Josh", "From Claude Code", "From brandLaunch", "Delegation from protoEngineer", "From the API", "From another agent".
- Time: local relative, full timestamp in a `Tooltip` (Work §2.3 rules). Message count only above 1.
- Status from `active` and `last_state` (`GET /api/chat/sessions/{id}` already returns both). Work tone map: Running `info` pulse, Needs you `warning`, Failed `error`; idle and done show a neutral dot and no badge.
- Live updates from the bus's `turn.started` / `turn.finished` events, already published for `origin: "a2a"` turns (`src/server/a2a.py`). These also make the tab appear the first time an outside conversation starts.

### 4.3 Noise and retention

One row per context id. Within a group: Needs you, Running, then newest. Seven days listed; **Show older** pages back 50 at a time. The list never deletes or hides threads; the task store's existing retention removes them. When the last thread leaves the window, the tab goes away.

### 4.4 Titles

1. `title` from the caller's metadata. `delegate_to` already takes a one-line `summary` written for the operator; send it as `title`.
2. The first line of the first user message, markdown stripped, clipped at 80 characters.
3. "Conversation from &lt;origin&gt;".

No agent-generated titles: a model call per thread, late, and it can disagree with the caller.

### 4.5 Continue in a new chat

Creates a `chat-` session, opens and selects it as a normal chat tab, and pre-fills the composer with `About the conversation "<title>" (<origin>): `, plus a hidden reference to the context id so the agent can `recall_session` it. Josh edits and sends. No toast (§3.8): the new tab opening, selected, is the feedback.

### 4.6 Links in

- Work's provenance line (D8 waiting-task detail, `apps/web/src/work/sections/tasks.tsx`): "Started in a conversation from Claude Code", as a `TextLink` to `#/conversations/<context_id>`.
- Needs you (`apps/web/src/chat/NeedsYouSection.tsx`): an item whose session isn't a `chat-` id gets a ghost **Open conversation** button.
- The route shows the Conversations tab (even if hidden), selects it and opens that thread, with its group expanded.

### 4.7 The tab is not a session

The Conversations tab is client-only. Its id is reserved (`conversations`) and never sent to the server, never written to the S6 chat list, never counted as a chat, and never offered by "unused blank chat" logic. The chat store keeps `currentSessionId` pointing at the last chat; a separate stage value (`chat` or `conversations`) says which is showing.

## 5. States and copy

| State | Where | Copy |
|---|---|---|
| No outside conversations | Tab bar | Tab not drawn. |
| Loading (first load) | Tab bar | Tab not drawn until the list arrives. |
| Loading (deep link) | List pane | `SkeletonGroup` of 4 rows under the header. |
| Failed (deep link, or refresh after the tab was shown) | List pane | `Alert` error: "Couldn't load conversations. Check the connection, then try again." Button: Try again. |
| Automated only | List pane | For you shows "Nothing started for you this week." Automated opens by default. |
| Thread, running | Thread | Badge Running; the last message shows `TypingIndicator`. |
| Thread, needs you | Thread | Callout warning: "This conversation is waiting for your answer." Button: Answer in Needs you. |
| Thread, gone | Thread | `Empty`: "This conversation is no longer stored." Muted: "The agent keeps conversations for a limited time. This one has been cleared." Button: Back to list. |
| Thread, failed | Thread | `Alert` error: "Couldn't load this conversation. Try again." |
| Hidden by Josh | Tab bar | Tab not drawn; it returns when something needs you. |

## 6. Component map (@protolabsai/ui)

| Piece | Component |
|---|---|
| Conversations tab | `TabBar` item with a new **pinned** flag (gap G4 below), `MessagesSquare` icon, `StatusDot`, `badge` count |
| Tab right-click menu | `Menu` / `MenuItem` |
| List header | `Eyebrow`, `h2` |
| Groups | group label `Eyebrow`; Automated is a disclosure |
| Rows | composite `<button>` (G3), `StatusDot`, `Badge`, `Tooltip` for time |
| Thread header | `Button variant="ghost"`, `Eyebrow`, `Badge` |
| Turns | `Conversation`, `Message`, `Markdown`, `TypingIndicator` |
| Needs you note | `Callout` warning |
| Footer bar | `Button` primary + ghost |
| States | `Empty`, `SkeletonGroup`, `Alert` |
| Phone | an item inside the existing `Drawer side="bottom"` (SessionSheet) |

**Gap G4 (design system, protoContent):** `TabBar` applies `onClose`, `onRename` and `onReorder` to every item; it has no way to make one item fixed. Proposed: `TabBarItem.pinned?: boolean`. A pinned item renders before the unpinned ones with a divider after it, has no ✕, ignores double-click rename, is not draggable, and keeps its place in the `responsive` collapsed menu (first row, outside the reorderable set). This is mine to build in protoContent and publish before the console slice adopts it.

All colour, spacing and type from `--pl-*` tokens. Meta text uses `--pl-color-fg-muted`; no status token is used as text colour (Work §2.2).

## 7. Accessibility

- The tab is a real `role="tab"` in the chat tablist, first in arrow-key order. Its accessible name includes the count, the status in words and "read-only".
- The tab's first appearance is announced once, politely: "Conversations tab added: 1 conversation from Claude Code." It isn't announced again when it returns or when the count changes.
- The Automated toggle is a disclosure (`aria-expanded`, `aria-controls`).
- Opening a thread moves focus to its `h2` (`tabindex="-1"`); **Back to list** returns focus to the row.
- Read-only is stated in text everywhere (§3.5); the missing composer is never the only signal.
- Running changes are announced politely in one shared live region, at most once every 10 seconds, and only while the Conversations tab is selected. While a chat is selected, the tab's accessible name carries the status and nothing is announced.
- Touch targets 44 px on phone.

## 8. Server change

Labelling the origin needs four fields stored on a context's **first turn**, sent by the caller in A2A message metadata:

| Field | Meaning |
|---|---|
| `caller` | Display name of whoever sent it (already sent; today it only reaches event frames, `src/a2a_impl/executor.py`). |
| `caller_kind` | `person` or `agent`. |
| `on_behalf_of` | A person's name, optional ("Josh"). |
| `title` | Optional thread title (`delegate_to` sends its `summary`). |

The list endpoint reads them back per context. A thread without them is Automated, "From another agent".

## 9. Build slices

Final. Approved by Josh, 2026-11-14. protoEngineer cards the protoAgent slices; D1 is mine in protoContent. Existing protoAgent paths were checked on protoLabsAI/protoAgent main on 2026-11-14, and D1's paths on protoContent main. Paths marked (new) don't exist yet. One path is marked (confirm): the file exists, but the exact edit site wasn't found.

| Slice | Repo | What | Files | Depends on |
|---|---|---|---|---|
| **D1** (card ds-tk8) | protoContent | `TabBarItem.pinned` (G4): fixed first, divider, no ✕, no rename, not draggable, first in the responsive menu. Story + test. Published to npm. | `packages/ui/src/navigation.tsx`, `packages/ui/src/TabBar.stories.tsx`, `packages/ui/src/menu.test.mjs` (the existing TabBar tests), `packages/ui-css/styles/navigation.css`, a changeset (new) | — |
| **S1** | protoAgent | Persist `caller`, `caller_kind`, `on_behalf_of`, `title` from the first turn's metadata; `delegate_to` sends `summary` as `title`, `caller_kind: agent`. | `src/a2a_impl/executor.py`, `src/graph/delegations.py` (confirm: the file sends no A2A metadata today; the coder finds where `delegate_to` builds the outgoing message), `tests/test_conversation_origin.py` (new) | — |
| **S2** | protoAgent | `GET /api/conversations?group=&before=&limit=`: non-`chat-` contexts minus the exclusions and private turns; each row has `session_id`, `title`, `origin_label`, `group`, `last_updated`, `turn_count`, `active`, `last_state`. | `src/operator_api/chat_routes.py`, `tests/test_chat_routes.py` | S1 |
| **C1** | protoAgent | API client and store: list, page older, live updates from `turn.started` / `turn.finished`, the tab's visible/hidden state (per device), and the stage value (`chat` or `conversations`, §4.7). | `apps/web/src/lib/api/chat.ts`, `apps/web/src/chat/conversations-store.ts` (new) | S2 |
| **C2** | protoAgent | Desktop Conversations tab: pinned `TabBar` item, appear/hide rules, right-click Hide, background-menu Show; selecting it shows the list pane with groups, Show older, loading/failed. | `apps/web/src/chat/ChatSurface.tsx`, `apps/web/src/chat/ConversationsPane.tsx` (new), `apps/web/src/chat/chat.css` | C1; waits for D1's published `@protolabsai/ui` (`npm:@protolabsai/ui@contains:protoLabsAI/protoContent@ds-tk8`) |
| **C3** | protoAgent | Thread inside the tab: header band with Back to list, turns, footer bar, needs-you callout, gone state; remembers the open thread. | `apps/web/src/chat/ConversationReader.tsx` (new), `apps/web/src/chat/ConversationsPane.tsx`, `apps/web/src/chat/chat.css` | C2 |
| **C4** | protoAgent | Continue in a new chat. | `apps/web/src/chat/chat-store.ts`, `apps/web/src/chat/ConversationReader.tsx` | C3 |
| **C5** | protoAgent | Phone: pinned Conversations item at the top of the session sheet; list and thread full screen; header title and ← List. | `apps/web/src/app/SessionSheet.tsx`, `apps/web/src/app/mobile-shell.css`, `apps/web/src/app/MobileShell.tsx`, `apps/web/e2e/mobile.spec.ts` | C3 |
| **C6** | protoAgent | Links in: `#/conversations/<id>` route (shows the tab even if hidden); the D8 provenance line becomes a link; Needs you items from non-`chat-` threads get **Open conversation**. | `apps/web/src/work/sections/tasks.tsx` (the D8 waiting-task detail: the provenance string is at line 200, just above the `WorkDetail` render at line 201), `apps/web/src/chat/NeedsYouSection.tsx`, `apps/web/src/app/palette/nav.ts` (the view router; add the `#/conversations/<id>` arm) | C3 |

Every console slice ships as a draft PR with screenshots at 1440 and 390, light and dark, per the house rule for Josh-reviewed surfaces. S1, S2 and D1 can run in parallel.

## 10. Tab decisions (revision 3)

Josh approved revision 3 for build on 2026-11-14 (relayed by Claude Code), with these five choices:

1. **Position:** pinned first, before the chats, with a divider after it.
2. **Hiding:** no ✕. Right-click **Hide**, per device. It comes back when something needs Josh or a link opens a thread.
3. **Label:** "Conversations".
4. **Badge:** the total count plus a status dot for running or needs you.
5. **Phone:** a pinned Conversations item at the top of the session sheet.

No questions are open.
