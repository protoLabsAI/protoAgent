# Answers to the spec's open questions (checked against core origin/main 47f627238 by Claude Code, 2026-11-08)

Design-system facts were checked against `@protolabsai/ui` **0.70.0**, the version `package-lock.json:1843` locks for
`apps/web`. Every component in §7.1 exists there with the import path the spec gives. The preview is rebuilt as a
standalone page on that same stylesheet; Claude Code publishes it.

1. **Freshness: there is a bus event, so don't poll.** Every ledger write publishes `directive.changed` with
   `{session_id, id}`: on record, on close (agent or operator) and on forget (`src/graph/directives.py:68-77, 225, 272,
   284`). `HOST.publish` is the console event bus (`src/server/plugin_wiring.py:362`), and the topic is listed in
   `docs/reference/plugin-events.md:47`. `useDirectives` should use the same pattern as `backgroundJobStore`
   (`apps/web/src/chat/backgroundJobStore.ts:134-169`): `onTopic("directive.changed", …)` from `lib/events.ts:221`,
   keep only frames whose `session_id` matches, then refetch `GET …/directives`. The event carries no row, so the view
   always refetches. It should also refetch on `onConnectionChange` reconnect (`lib/events.ts:234`) so a dropped stream
   can't leave the list stale. A refresh at turn end is not needed.

2. **Mount point: `ChatSessionSlot.tsx`, not `ChatSurface`.** The dock renders `<GoalRunStrip sessionId={sessionId} />`
   and then `<BackgroundWorkStrip sessionId={sessionId} />` (`apps/web/src/chat/ChatSessionSlot.tsx:1920-1921`). Both
   come above the roster and the composer. `.chat-bgwork` and its `--idle` modifier are in `apps/web/src/chat/chat.css:1167-1199`. The
   spec assumed only one sibling, but the goal strip is already above it. "Directly above BackgroundWorkStrip" therefore puts the
   ledger between the goal strip and background work. To keep the "allowed, then doing" reading, mount `DirectiveStrip`
   **first, above `GoalRunStrip`**. Copy the `.chat-bgwork` rules as `.chat-ledger` (the preview does exactly that).

3. **`/directives` is a server command today, and the console can take over the bare form.** The server handles it before the
   turn and answers with a text reply (`src/server/chat_dispatch.py:212-222, 573-579`, text from
   `src/graph/directives.py:377-425`). Client commands run first in the composer (`ChatSessionSlot.tsx:419-440`). A
   `run` that returns `false` lets the token go on to the server unchanged (`apps/web/src/ext/slashRegistry.ts:57-75`).
   Build: `registerSlashCommand({ name: "directives", … })` in `apps/web/src/chat/coreSlashCommands.ts`, following `/new`
   and `/clear` (`:34-56`). With an empty `ctx.rest` and a session, open the drawer and return `true`. With any argument
   (`revoke <id> …`), return `false` so the server path stays. Non-console clients (A2A, `/v1`) keep the text reply.
   This also gives the drawer a ⌘⇧K palette row for free, because the palette lists client commands
   (`apps/web/src/app/chatSlashPalette.ts:1-20`).

4. **There is no chat overflow menu.** On desktop the chat header is the DS `TabBar`. Its only menu is the **right-click
   context menu on a tab** (`ChatSurface.tsx:325-349`, entries in `apps/web/src/contextMenu/registrations.tsx:218-275`:
   Rename, Incognito, Export, Close…). On phones the tab strip is hidden (`ChatSurface.tsx:57`). `MobileShell`'s header
   holds Back, the nav Menu, the session switcher, Search commands and New (`apps/web/src/app/MobileShell.tsx:79-152`),
   and none of them is a per-chat menu. The closest entry points are (a) a "Standing instructions" item in the
   `chat-tab` context menu, added next to Export, and (b) the `/directives` palette row from Q3, which phones reach from
   the header's "Search commands" button. Use both, and correct §3.3 to name them.

5. **`reason` is optional, but an empty reason is not stored as empty.** The route stores `(reason or "").strip() or
   "revoked by the operator"` (`src/operator_api/chat_routes.py:1496`). The `/directives revoke` command does the
   same (`src/graph/directives.py:420`). The API sets no length limit; the store keeps the first 2,000 characters
   (`MAX_NOTE_CHARS`, `directives.py:49, 269`). `reason` travels as a **query parameter** on the DELETE, so encode it
   with `encodeURIComponent`, and cap the textarea at 2,000. **The build must hide that default.** Otherwise every reasonless withdraw shows
   "Note: revoked by the operator". When `closed_by === "operator"` and `close_reason === "revoked by the operator"`,
   don't show a Note.

6. **The 409 body does not include the closed entry.** It is FastAPI's plain `{"detail": "directive dir-4a91c2 is already
   fulfilled"}` (`chat_routes.py:1501-1502`, message from `directives.py:266`). `request()` throws
   `ApiError(status, detail)` (`apps/web/src/lib/api/http.ts:18-21, 188`). Refetch on 409 and build the §4.3 sentence
   from the fresh row, as the spec's fallback says. A 404 also arrives as `ApiError` with `status === 404`.

7. **There is a formatter, but it lives in `app/`.** `formatTimestamp` in `apps/web/src/app/tasks.ts:58-80` already
   produces "today at …" and "yesterday at …", and anything older as `Intl` "Nov 7, 5:14 PM" in the user's locale.
   `chat/` deliberately imports nothing from `app/` (`chat/slashDispatch.ts:77`), so **move it to
   `apps/web/src/lib/format.ts`** and import it from both places. It needs one change for the spec's older-date form
   "7 Nov at 17:14": a joined "{date} at {time}". For the tooltip, reuse `localStampTitle` (`lib/format.ts:68`). It
   gives the full local stamp plus the raw ISO, and `parseInstant` treats a value with no offset as UTC. `created_at`
   comes as `2026-11-07T17:14:02+00:00`, not `…Z` (`directives.py:64-65`).

8. **Yes, mirror the counter pattern, but the request has to carry the session.** `uiStore` has `backgroundJobsRequest: number` plus
   `openBackgroundJobs()`, a counter that the widget watches (`apps/web/src/state/uiStore.ts:180-184, 593`;
   `apps/web/src/app/BackgroundJobs.tsx:191-196`). The ledger drawer needs a session, because it is opened from a tab's
   context menu or the palette, which may not be the visible chat. Use `directivesRequest: { sessionId: string; n:
   number } | null` with `openDirectives(sessionId)`. Add it to the `partialize` exclusion list (`uiStore.ts:606`), or
   it will persist.

9. **Use `useIsMobile()`; `Drawer` does not choose a side.** The hook is in `apps/web/src/lib/useIsMobile.ts:5-6`
   (`(max-width: 767px)`, the shell switch), and `ChatSurface.tsx:57` already uses it. `Drawer` only applies the `side`
   it is given (`@protolabsai/ui` 0.70.0 `src/overlays.tsx:197-229`). Its bottom sheet is real: `.pl-drawer--bottom` is
   full width, `height: auto` and capped at `85dvh`. The console has precedents: `SessionSheet.tsx:37` (`side="bottom"`)
   and `AppDrawer.tsx:51` (`side={mobile ? "left" : "right"}`). Pass `side={mobile ? "bottom" : "right"}`.

10. **Yes.** At the mount point the slot already has `sessionId` and passes it to both strips (`ChatSessionSlot.tsx:1920-1921`).
    Render `<DirectiveStrip sessionId={sessionId} />` the same way.

11. **There is no link to a directive, but the wait for a chat can be found exactly.** A watch record has no directive
    field (`src/graph/watches/types.py:60-108`), and neither does a scheduler job. Nothing in `scheduler/` or
    `graph/watches/` mentions directives. A chat can have **at most one pending `wait`**, and its job id is fixed:
    `wait:{session_id}` (`src/tools/scheduler_tools.py:259`, where a new wait replaces the old one). Watches carry
    `run_session` (`types.py:97`). So the confirm can honestly say "This chat also has a pending wake-up: '{then}' at
    {time}. Withdrawing doesn't cancel it. [Cancel it]". The data comes from `GET /api/scheduler/jobs` (match `id ===
    "wait:"+sessionId`) and `GET /api/watches` (filter `run_session`). The cancel action calls `DELETE
    /api/scheduler/jobs/{id}` or `DELETE /api/watches/{id}` (`src/operator_api/routes.py:741, 844, 853`). This applies
    to the chat, not to one entry, so the copy must not say the wait "belongs to" the instruction. It is optional. §4.4's
    copy is correct either way. If Josh wants the wake-up shown, card it as a follow-up slice.

## Corrections to the spec

- **§3.3, §6 entry points:** there is no chat overflow `Menu` (Q4). Use the tab context menu plus the `/directives`
  client command and its palette row.
- **§3.1 placement:** `GoalRunStrip` is already above `BackgroundWorkStrip`, and the dock is in `ChatSessionSlot.tsx` (Q2).
- **§4.3 Note line:** a withdraw with no reason stores `"revoked by the operator"`, and the view must not show it (Q5).
- **§6 Drawer focus return:** `Drawer` traps focus but **does not return it to the trigger** on close. `useFocusTrap`
  focuses the first control and wraps Tab, with no restore (`overlays.tsx:27-55`). The build must save
  `document.activeElement` on open and refocus it on close (the strip's View button, or the composer when opened from
  the palette).
- **§5.6 Escape in the withdraw region:** the Drawer closes on a **window** `keydown` Escape unless the event is
  `defaultPrevented` (`overlay-layer.ts:32-37`). The region's Escape handler must call `e.preventDefault()`. Otherwise one
  Escape closes both the region and the drawer.
- **§5.4, §7.1 `ScrollArea`:** the Drawer already wraps its children in `.pl-drawer__body` with `overflow-y: auto`
  (`overlays.tsx:263`). Another `ScrollArea` would nest a second scroller, so drop it.
- **§6 headings:** `AccordionItem`'s `title` renders inside its trigger `<button>` (`navigation.tsx:875-915`). "History"
  therefore can't be an `h3`; only "In force" is a real heading.
- **§4.5, §6 focus to the 409/404 callout:** `Callout` accepts only `tone`, `title` and `children`, with no `id`,
  `tabIndex` or ref (`primitives.tsx:108-122`). Wrap it in a `<div tabIndex={-1}>` and focus the wrapper.
- **§5.7 failed load:** `Alert status="warning"` renders `role="alert"`, which is assertive (`data.tsx:255`). That's
  acceptable for a failed load the operator opened. The drawer-only "Showing what was loaded at…" note correctly uses
  `Callout`, which has no live role.
- **§7.1 `Badge`:** the prop is `status` (default `"neutral"`), not `tone`. Plain `<Badge>done</Badge>` is right.
- **Brief data shape:** ids are `dir-` plus 6 hex characters (`directives.py:212`), not `d_…`. Timestamps end in `+00:00`.
  Neither is ever shown.

## For Josh (left open)

12. **Inline confirm or dialog?** This spec uses an inline confirm inside the entry, to avoid a dialog stacked over a drawer on the phone. `ConfirmDialog` is the alternative.
13. **Transcript note.** When you withdraw an entry, should a short system line appear in the chat ("You withdrew: "once #4801…"")? It would make the change visible in the transcript the agent reads, not only in the ledger.
14. **History alone.** Is it right that the strip stays hidden when only history exists, and history is reachable from the overflow menu and `/directives`?

(With Q4's answer, "the overflow menu" in 14 means the tab context menu and the palette.)
