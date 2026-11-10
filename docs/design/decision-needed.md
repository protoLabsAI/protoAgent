# "Decision needed" console item — design spec

> **Phone update (Josh, 2026-11-09):** The phone header button described below was withdrawn and its unused composite removed. Swipe up or the hamburger Work item opens the Work sheet; the hamburger alert dot signals pending work. The earlier phone-button details are historical.

Status: **APPROVED, ready to build** (Josh, 2026-11-08, revision 1: "yeah looks good"). Placement: a Decisions `UtilityWidget` pill in the desktop/tablet bottom utility bar, and a header icon button with a count on mobile (the bottom quick-bar was retired, ADR 0086 D1). Owner: designSystem. Issue: #4725.

- Surface: core console, `apps/web` (issue #4725)
- Requested by: Josh, 2026-11-08
- Status: approved (revision 1), see the status line above.
- Uses: `@protolabsai/ui` and `@protolabsai/design` only. No new design-system component is needed: desktop uses the existing `UtilityWidget`, the phone adds one button to the existing `MobileShell` header. (Revision 1's floating-button idea was withdrawn by Josh after checking the code; see §2.2.)

## 1. What this item answers

The item answers one question from anywhere in the console: **is an agent waiting on me, and what does it need?**

It lets Josh do two things:

1. Answer in one action, by picking one of the options the agent offered.
2. Open the chat the decision came from, if he wants the context first.

A pending decision is a **request**, not an alarm. Nothing in this spec uses warning or error colour for a decision that is simply waiting.

## 2. Placement

> **Revision 1 (Josh, 2026-11-08):** the top-bar button is replaced. On desktop and tablet, decisions live in a **utility-bar pill**. On a phone, they live in a **header icon button with a count, beside New chat**. The floating button considered earlier is withdrawn: the bottom quick-bar was retired (ADR 0086 D1) and the composer owns the bottom. The desktop notification and the card in the source chat stay as they were.

Each channel has one job:

| Channel | Job | When |
|---|---|---|
| **Utility-bar "Decisions" pill** (≥ 768 px) | The always-available place to see and answer every pending decision, from every fleet member. | Always rendered. Quiet with nothing pending; carries a count while something is pending (§2.1). |
| **Header "Decisions" button** (< 768 px) | The same, on the phone, where there is no utility bar. | Only while something is pending (§2.2). |
| **Desktop notification** | Reach Josh when the console window is hidden or in the background. | Once per new decision, through the existing `notifyIfHidden` (`apps/web/src/lib/notify.ts`). Unchanged. |
| **The decision in its own chat** (context) | Show the decision where the agent stopped, so reading the chat explains itself. | Always, as the last item of the parked turn. Unchanged. |

The hub sees every fleet member, so the count is fleet-wide. A standalone agent window shows only its own decisions.

### 2.1 Desktop and tablet (≥ 768 px): a utility-bar pill

**Slot: directly after `PaletteButton`, before `BackgroundJobs`.** The cluster becomes:

```
Settings · Palette · Decisions · Background jobs · Feed (Activity) · plugin widgets…
```

Why this slot:

- A pending decision is the only item in the cluster that is **blocking an agent**. Background jobs and the feed report on work that is moving on its own. The blocking item comes first.
- Settings and Palette keep their positions, which operators reach for by position (the comment on `PaletteButton` in `App.tsx`).

**Quiet when nothing is pending, never hidden.** If the pill appeared and disappeared, Background jobs, the feed and every plugin widget would jump sideways each time a decision arrived or cleared. That breaks reaching for them by position, which is the reason Settings holds the far-left slot. So:

| Case | Pill |
|---|---|
| Nothing pending | The icon only, no badge. Ghost, like its neighbours. Hover info: "Decisions · nothing is waiting on you". Clicking it opens the dialog with "Nothing is waiting on you." |
| 1 or more pending | The icon plus a `Count` badge ("1" … "9", then "9+"), the same badge shape as the feed. Default `Count` style, never `activity-badge--alert` and never warning colour. |
| Loading (first load) | Same as nothing pending. It never flashes "0". |

**Shape.** The pill is a `UtilityWidget` (`apps/web/src/app/UtilityWidget.tsx`), as Josh asked:

| Prop | Value |
|---|---|
| `icon` | `MessageCircleQuestion` (lucide), 14 px, matching the cluster |
| `badge` | `<Count>{n > 9 ? "9+" : n}</Count>` while pending, otherwise none |
| `label` (aria-label) | "Decisions, 2 waiting on you" · "Decisions, 1 waiting on you" · "Decisions, nothing waiting" |
| `info` (hover and focus) | The top decision (§3.1 order): "**protoEngineer:** The release PR for v0.227.0 (#4723) is green, but QA gave a WARN…" cut at 120 characters, then "and 1 more" on a second line when there are several. With nothing pending: "Decisions · nothing is waiting on you". |
| `dialogTitle` | Decisions waiting on you |
| `dialogWidth` | `min(520px, 94vw)` |
| `testId` | `decisions-widget` |

The `info` preview is a convenience. Everything in it is also in the dialog, so nothing is hover-only.

**The dialog** holds the panel content from §3, unchanged: the intro line, the cards in §3.1 order, every state in §5. `UtilityWidget` mounts the body only while open. That is fine here, because the count and the info come from the fleet-wide `useDecisions()` store, not from the body.

### 2.2 Phone (< 768 px): a header icon button beside New chat

Below 768 px the console runs its own chat-first `MobileShell` (`apps/web/src/app/MobileShell.tsx`). The bottom quick-bar was retired (ADR 0086 D1) and the composer owns the bottom of the screen, so nothing floats there. Decisions go in the header.

**The header today** (`.mshell-head`, chat root), left to right:

```
[Menu]  [ Chat title ⌄ ]  [Search]  [New chat]
```

Every header action button is a DS `<Button variant="ghost" size="md" icon>`: at least 44 × 44 px, no border, no fill, `--pl-color-fg` icon at 20 px. The title is `flex: 1; min-width: 0` and ellipsizes (`.mshell-title-text`).

**With a decision pending:**

```
[Menu]  [ Chat title ⌄ ]  [Search]  [? 2]  [New chat]
```

- **Slot:** directly before New chat, so it sits beside New chat and New chat keeps its far-right position, which thumbs reach for by position.
- **What it is:** the `MessageCircleQuestion` icon at 20 px and a default `Count` **beside** it (not overlapping the icon), inside one button. "9+" above 9.
- **Size:** at least 44 px tall. At least 44 px wide, growing with the count to about 56 px for "9+". It never shrinks: `flex: 0 0 auto`.
- **Quiet styling:** `<Button variant="ghost" size="md" icon>`, matching its neighbours exactly. The `Count` is the DS default pill. No accent, no warning colour, no pulse or dot.
- **When it shows:** only while at least one decision is pending (Josh's rule), so the header never carries an empty slot. During the first load it stays hidden (§5.2).
- **Tapping it** opens the Decisions panel as the existing bottom sheet (`Drawer side="bottom"`, up to 85dvh). Closing the sheet returns focus to the button, or to New chat if the button has gone because nothing is left.

**Pushed views** (Knowledge, Memory, plugin surfaces). The header there is `[Back]  Title  [spacer]`, where the right-hand spacer (`<span className="mshell-head-spacer" aria-hidden />`, `min-width: 44px` per `mobile-shell.css:32`) balances the leading back-chevron button. **While a decision is pending, the Decisions button takes the spacer's place.** It is 44 px wide with no count and grows to ~56 px at "9+", so the title shifts slightly off-centre when a count shows. A decision is reachable from every screen. With nothing pending the spacer comes back. (Q19 asks Josh to confirm.)

**Fit at 320 px.** The header has 4 px padding each side and a 4 px gap. At 320 px with a decision pending:

| Item | Width |
|---|---|
| Padding (2 × 4 px) | 8 px |
| Menu, Search, New chat (3 × 44 px) | 132 px |
| Decisions, count "9+" (worst case) | ~56 px |
| Gaps (4 × 4 px) | 16 px |
| **Left for the title** | **~108 px** (chevron and padding included, so about 80 px of text) |

So the title truncates, for example "Cut v0.2…". **The title gives way, never a button:** the title already has `min-width: 0` and ellipsis; every header button keeps its 44 px minimum. The full title is still one tap away in the session sheet. A long chat title like "What we learned shipping v0.226" shows as "What we…" at 320 px and as about "What we learned sh…" at 390 px (about 180 px left for the title there). The preview checks both widths.

**The rest of the phone is unchanged:** the composer, the strips above it, the keyboard handling (`useKeyboardInset`) and the bottom safe-area inset are not touched, because nothing is added to the bottom.

### 2.3 What is unchanged

- **Desktop notification:** as §4.5. Clicking it brings the console forward and opens the Decisions dialog (desktop) or the sheet (phone).
- **The card in the source chat:** as §3 and §5.5. The same `DecisionCard`.
- **Activity feed events:** as §5.9.

### 2.4 Why not the other places (kept from the first draft)

- **Rail badge.** The `SurfaceRail` belongs to one agent's surfaces and is gone on a phone.
- **A composer strip.** `BackgroundWorkStrip` and the directive-ledger strip are scoped to one chat; a decision must be seen from every chat and agent.
- **The Activity feed.** It is a read-only history; decisions appear there as events but are answered in their own pill.
- **A `Banner`.** Too loud for a request, and several agents would stack several banners.
- **The top bar** (the first draft). Replaced by Josh's revision: the utility bar is where the console's other "something for you" widgets already live.

## 3. Information architecture

There are three levels.

1. **Entry point.** The utility-bar pill (desktop) or the header button (phone). It answers "is anything waiting, and how many?"
2. **Decisions panel.** It lists every pending decision as a card, each answerable in place.
   - Desktop: the `UtilityWidget` dialog, `min(520px, 94vw)` wide. The body scrolls inside the dialog.
   - Phone: `Drawer side="bottom"`, up to the component's 85dvh cap.
   - Title: **Decisions waiting on you**.
3. **Decision card.** One decision: who asked, the question, the options, what happens if nobody answers, and a link to the chat. The same card renders in the panel and in the source chat.

### 3.1 Order in the panel

1. Decisions with an expiry, soonest expiry first.
2. Then decisions without an expiry, oldest first.

Cards from different agents are not grouped under agent headings. Each card names its agent, and a flat list keeps "what's most urgent" at the top. The pill's hover `info` shows the first card in this order.

### 3.2 Fields on a decision card

| Order | Field | Shown as |
|---|---|---|
| 1 | Agent | `Avatar` (size sm) and the agent name, for example "protoEngineer". A `StatusDot` follows only when the agent is offline (§5.7). |
| 2 | When it was asked | "asked 14:45", or "asked 7 Nov at 14:45". Full timestamp in a `Tooltip`. Muted text, same line as the agent. |
| 3 | The question | The agent's words, in the largest text on the card. Plain text, line breaks kept, never reworded. Clamped at 6 lines in the panel with **Show full question** (same pattern as the ledger's long quote, §5.5 of `directive-ledger-view.md`). |
| 4 | Options | Two to four buttons, in the agent's order (§4.3). |
| 5 | If nobody answers | "If nobody answers by 15:45, protoEngineer will {default action}." Hidden if there is no expiry. |
| 6 | Where it came from | `TextLink`: "Open chat: {chat title}". It switches to the agent and the chat, then closes the panel. |
| — | Decision id, session id, raw status | **Never shown.** |

## 4. Copy

All copy uses plain words. It never shows `pending`, `parked`, `expired`, `blocking`, a decision id or a session id.

### 4.1 Entry points

| Case | Desktop pill (visible) | Phone header button (visible) | Accessible name |
|---|---|---|---|
| Nothing pending | icon | not rendered | "Decisions, nothing waiting" |
| 1 pending | icon `1` | icon `1` | "Decisions, 1 waiting on you" |
| N pending | icon `N` (`9+` above 9) | icon `N` (`9+` above 9) | "Decisions, N waiting on you" |
| Phone `title` | — | "Decisions waiting on you" | — |
| Hover info (desktop) | **{Agent}:** {question, cut at 120 characters}… / and {N−1} more | — | — |
| Hover info, nothing pending | Decisions · nothing is waiting on you | — | — |

### 4.2 Panel

| Element | Text |
|---|---|
| Title | Decisions waiting on you |
| Intro (one line) | Your agents stopped to ask you these. Pick an answer and the agent carries on. |
| Card meta | {Agent} · asked {when} |
| If nobody answers | If nobody answers by {time}, {Agent} will {default action}. |
| Open chat link | Open chat: {chat title} |
| Long question toggle | Show full question / Show less |
| Nothing pending (desktop dialog opened from the quiet pill) | Nothing is waiting on you. When an agent stops to ask you something, it shows up here. |
| Nothing left (after the last answer, panel still open) | That's everything. Nothing is waiting on you. |

The default action is written by the agent as a verb phrase ("keep holding the release"). The console adds the subject and the time. If the agent gave none, the line reads "If nobody answers by {time}, {Agent} will carry on without this step." — the backend must define that fallback (Q6).

### 4.3 Options

- Each option is a `Button size="md"`. The label is the agent's short option text, for example "Merge with the WARN".
- An option can carry one line of explanation from the agent. It renders under the label as muted text inside the button's accessible description.
- **Variant:** every option is `variant="default"` by default (`Button` has no `secondary` variant). The agent may mark one option as **recommended**. That option is `variant="primary"` and gets a lowercase `Badge` "suggested". The console never picks a recommendation itself.
- No option is ever `danger`. Choosing is not an error. If an option is destructive or outward-facing, the agent says so in the option's explanation.
- Layout: a vertical stack, full width, in the panel and on the phone. Labels wrap; they are never cut off.
- A ghost button after the options: **Reply in chat instead**. It opens the chat with the composer focused, for an answer that isn't one of the options.

### 4.4 Results

| Case | Text | Where |
|---|---|---|
| Sending | The chosen button shows `loading`. The others are disabled. | On the card |
| Sent | **Sent: "{option}".** {Agent} is picking it back up. | Replaces the options for 4 seconds, then the card leaves the list. A neutral toast repeats it if the panel was closed by then. |
| Answered somewhere else (409) | Already answered: you chose "{option}" in the chat at {time}. Nothing else changed. | `Callout tone="info"` in place of the options, then the card leaves when the panel closes. |
| No longer needed | {Agent} doesn't need this any more. {reason from the agent, if any} | `Callout tone="neutral"` in place of the options. Leaves when the panel closes. |
| Expired | No answer by {time}, so {Agent} went ahead and {default action, past tense}. | `Callout tone="neutral"` in place of the options. Leaves when the panel closes. |
| Couldn't send | Couldn't send your answer. Nothing changed, and {Agent} is still waiting. | `Alert status="warning"` on the card with **Try again**, which resends the same option. The options stay enabled. |

"Expired" never says "missed" or "failed". The agent did the stated thing, which is the system working as designed.

### 4.5 Desktop notification

| Field | Text |
|---|---|
| Title | {Agent} needs a decision |
| Body | The question, cut at 240 characters (the existing `notifyIfHidden` limit) |

Clicking it brings the console forward and opens the Decisions panel (the dialog on desktop, the sheet on the phone). One notification per decision, never repeated by a poll or a reload.

### 4.6 The main example (v0.227.0)

> **protoEngineer** · asked 14:45
>
> The release PR for v0.227.0 (#4723) is green, but QA gave a WARN: `uv.lock` had its revision field rewritten. Nothing else changed. My release skill only merges on PASS or a WARN you accept. Merge it?
>
> [ **Merge with the WARN** ] `suggested` — The lock rewrite is harmless. I'll merge and cut the release.
> [ Hold the release ] — I'll leave #4723 open and do nothing else.
> [ Re-run QA first ] — I'll re-run the QA check and ask again if it still warns.
> Reply in chat instead
>
> If nobody answers by 15:45, protoEngineer will keep holding the release.
> Open chat: Cut v0.227.0

## 5. States

Every state below is in the preview.

### 5.1 None pending

- Desktop: the pill is quiet (icon, no badge). Opening it shows "Nothing is waiting on you…" (§4.2).
- Phone: the header button is not rendered. The header is exactly today's: Menu, title, Search, New chat (the spacer on pushed views).
- If the panel is open when the last decision clears, it shows "That's everything" for the rest of that view. On the phone the header button has gone, so closing the sheet returns focus to New chat.
- No notification, no empty badge, no "0".

### 5.2 Loading

- Desktop: the pill stays quiet until the first load finishes, so it never flashes "0".
- Phone: the header button stays hidden until the first load finishes.
- The panel, if opened from a notification before the load finishes, shows two `Skeleton lines={3}` blocks.

### 5.3 One pending

- Desktop: the pill shows `1`. Its hover info shows the question.
- Phone: the header button (icon `1`) appears before New chat.
- Panel: one card, fully expanded.
- Source chat: the same card is the last item of the parked turn.

### 5.4 Several, from different agents

- Desktop: the pill shows `2` (or `3`…). Hover info shows the first card's question and "and 1 more".
- Phone: the header button shows icon `2`.
- Panel: cards ordered per §3.1, with a `Divider` between them. Each card names its own agent. For example: protoEngineer (release, expires 15:45) then Cindi (blog post publish time, no expiry, asked earlier).
- Answering one card never moves focus out of the panel, and the others stay put.

### 5.5 Answered

1. Josh picks an option. The button shows `loading`.
2. On success, the card shows the "Sent" line (§4.4) and a `StatusDot status="success"`. This is the only place success colour appears, and the words carry the meaning.
3. After 4 seconds the card leaves. Focus moves to the next card's question, or to the panel title if none are left. The count on the pill or the header button updates.
4. In the source chat, the card stays in the transcript, frozen, with "You chose "{option}" · 14:52". The agent's resumed turn follows it.

### 5.6 Expired, or no longer needed

- The backend closes the decision (expiry, or the agent resolved it another way).
- If the panel is closed, the decision drops out of the count silently. Nothing announces it. On the phone, the header button disappears if it was the last one.
- If the panel is open, the card swaps its options for the neutral callout from §4.4 and leaves when the panel closes. It never vanishes under Josh's pointer.
- In the source chat, the card freezes with the same sentence.

### 5.7 Agent offline

- The card shows `StatusDot status="neutral" label="offline"` after the agent name.
- The options stay enabled **if** the backend holds the answer until the agent restarts (Q3). The card adds: "{Agent} is offline. It gets your answer when it starts again." Sent copy becomes "Saved: "{option}". {Agent} gets it when it starts again."
- If the backend can't hold answers, the options are disabled and the line reads: "{Agent} is offline. Start it to answer." with a ghost **Start** button, matching the existing stopped-agent banner action.
- An offline agent's decision still counts on the pill and the header button: it is still waiting on Josh.
- Offline is never shown as an error or in warning colour.

### 5.8 Couldn't send

- The `Alert status="warning"` from §4.4 appears on the card, above the options. Focus moves to it.
- The options stay enabled so Josh can retry or pick another one.
- A 409 is not a failure: it uses the info callout.

### 5.9 Other surfaces

- **Activity feed.** "protoEngineer asked for a decision" and "You answered protoEngineer's decision" appear as ordinary feed events. They link to the panel. They are not answerable there. A decision does not also raise the feed's own badge, so one decision never shows two counts in the same cluster (Q14).
- **Fleet hub attention list.** Uses the same card, if the hub has an attention list (Q8).

### 5.10 Themes and sizes

- Light and dark use tokens only: no literal colours, spacing values or radii.
- Question text uses `--pl-color-fg`. Meta, option explanations and the "if nobody answers" line use `--pl-color-fg-muted`, which measures 7.24:1 (dark) and 7.73:1 (light) on `--pl-color-bg-raised`, as measured for the directive ledger. Do not use `--pl-color-fg-subtle` for any text on the card: it fails at 3.42:1 on the light surface.
- The `Count` on the pill and the header button uses the DS default pill. It is not tinted warning.
- **1440 px:** utility-bar pill, `size="xs"` like its neighbours, which keeps a 24 × 24 pointer target (WCAG 2.5.8) through the DS hit-area. Dialog `min(520px, 94vw)`.
- **768–1023 px (tablet):** the same pill and dialog.
- **390 px and 320 px:** header button at least 44 × 44 px, on the header's `--pl-color-bg-raised` surface like its neighbours. Its icon uses `--pl-color-fg`, which clears the 3:1 non-text minimum (WCAG 1.4.11) in both themes. Bottom sheet; option buttons full width at `size="md"`. At 320 px the chat title truncates, never a button (§2.2).

## 6. Accessibility

- **Desktop pill.** A DS `Button` (through `UtilityWidget`) with the accessible name from §4.1. Its `Tooltip` opens on focus as well as hover. It should expose `aria-haspopup="dialog"`; `UtilityWidget` doesn't set it today (Q12).
- **Phone header button.** A DS `<Button variant="ghost" size="md" icon>` with the accessible name from §4.1 (the visible count is inside the name, so a screen reader hears "Decisions, 2 waiting on you", not "2"), `aria-haspopup="dialog"` and `aria-expanded` while the sheet is open. Icon and `Count` are `aria-hidden`. DOM order matches screen order: Menu, title, Search, Decisions, New chat. The DS Button provides the visible keyboard focus ring (Q24 resolved).
- **Live announcement.** One app-level `role="status"` region announces each new decision once, politely: "protoEngineer needs a decision." It is never `role="alert"`; a request does not interrupt (WCAG 4.1.3). The pill and the header button are not live regions themselves, so a count change is never announced twice.
- **Panel.** `Dialog` (desktop) and `Drawer` (phone) provide the focus trap, Escape to close and focus return to the pill or the header button. The dialog name is the title.
- **Card.** Each card is an `article` labelled by "{Agent}: {first words of the question}". The options are a `role="group"` labelled "Answer". Option explanations are linked through `aria-describedby`.
- **Results.** "Sent" and the callouts are inside a polite live region on the card. Focus moves as in §5.5 and §5.8.
- **Keyboard.** Every control is a DS `Button` or `TextLink`. Nothing is hover-only: the pill's info repeats the first card.
- **Motion.** The header button appears and disappears with no slide or pulse. The 4-second "Sent" hold has no animation. Overlays inherit the DS reduced-motion handling.
- **Reflow and zoom.** At 200% text size the header buttons keep their size and the title truncates further; the full title stays reachable in the session sheet.
- **Colour.** No state relies on colour: every state has a sentence.
- **Notification permission.** The console asks for it only the first time a decision arrives, through `notifyIfHidden`. Refusing it changes nothing else.

## 7. Behaviour rules for the build

- **One action.** A tap on an option sends it. There is no confirm step. The agent writes options so that each is safe to pick (Q4 asks whether some options need a confirm).
- **No duplicates.** A decision is keyed by its id. The pill, the header button, the panel, the source chat and the notification all read one store.
- **Freshness.** The count updates from the `decision.changed` bus event, not a slow poll (Q1, answered). A decision that waits 5 minutes before Josh can see it repeats the incident.
- **Closed means closed.** Once the server says a decision is answered, expired or withdrawn, no control on any surface can answer it.
- **One entry point per size.** At ≥ 768 px only the pill renders; below 768 px only the header button. The switch follows the console's existing 768 px breakpoint, so a window resized across it never shows both.

## 8. Components

### 8.1 Design-system components used (all exist today)

| Component | Import | Use |
|---|---|---|
| `Button` | `@protolabsai/ui/primitives` | Options (default or primary, md), Reply in chat instead (ghost), Try again, Start, Show full question. Also inside `UtilityWidget`. |
| `Count` | `@protolabsai/ui/primitives` | The number on the pill (as `UtilityWidget`'s `badge`) and beside the icon in the phone header button |
| `Badge` | `@protolabsai/ui/primitives` | "suggested" on the recommended option |
| `Avatar` | `@protolabsai/ui/primitives` | The agent on each card |
| `Callout` | `@protolabsai/ui/primitives` | Answered elsewhere (info), no longer needed and expired (neutral) |
| `Divider` | `@protolabsai/ui/primitives` | Between cards |
| `TextLink` | `@protolabsai/ui/primitives` | Open chat |
| `StatusDot` | `@protolabsai/ui/data` | Sent (success), offline (neutral) |
| `Alert` | `@protolabsai/ui/data` | Couldn't send |
| `Skeleton` | `@protolabsai/ui/data` | Loading |
| `ScrollArea` | `@protolabsai/ui/data` | Sheet body on the phone |
| `Dialog` | `@protolabsai/ui/overlays` | The desktop panel (through `UtilityWidget`) |
| `Drawer` | `@protolabsai/ui/overlays` | The phone panel (bottom sheet) |
| `Tooltip` | `@protolabsai/ui/overlays` | The pill's info (through `UtilityWidget`), full timestamps |
| `useToast` | `@protolabsai/ui/overlays` | "Sent" when the panel has closed |
| `UtilityBar` | `@protolabsai/ui/app-shell` | Unchanged; the pill joins its `start` cluster |

`Popover` and `Header` from the first draft are no longer used.

### 8.2 Console composites (in `apps/web`, not the design system)

| Composite | Purpose |
|---|---|
| `UtilityWidget` (exists) | The desktop pill and its dialog. Needs a small addition: controlled `open` / `onOpenChange`, so a notification click or a link can open it (Q11), and `aria-haspopup="dialog"` on the pill (Q12). |
| `DecisionsWidget` (new) | Wraps `UtilityWidget` with the icon, `Count` badge, label and info from §2.1. Mounted in `App.tsx` between `<PaletteButton />` and `<BackgroundJobs />`. |
| Phone Decisions header button (withdrawn) | The phone entry point: a DS `<Button variant="ghost" size="md" icon>` with an `aria-label` ("Decisions, N waiting") and a `Count` badge beside the icon, wired to `useDecisions()`. `MobileShell` renders it before New chat on the chat root, and in place of the right-hand spacer on pushed views (§2.2). Renders nothing when the count is 0 or during the first load. Opens `DecisionsPanel` in `Drawer side="bottom"`. |
| `DecisionsPanel` | The panel body: intro, ordering, the "nothing" lines. Rendered inside the `UtilityWidget` dialog on desktop and inside `Drawer side="bottom"` on the phone. |
| `DecisionCard` | One decision and every state in §5. Used in the panel and in the source chat transcript. |
| `useDecisions()` | Fleet-wide store: load, `decision.changed` push updates, answer, 409 handling, `notifyIfHidden(title, body?)` once per id. Drives the count for both entry points. |

The composites use tokens only and pass `ds_check`. The header button's only new CSS is layout: `flex: 0 0 auto` and a `var(--pl-space-1)` gap between icon and `Count`, in `mobile-shell.css`. It never restyles a `.pl-*` class.

### 8.3 The phone header button needs no DS contribution

- It joins an existing app-owned control family: the `MobileShell` header buttons (DS `<Button variant="ghost" size="md" icon>`, `MobileShell.tsx`). It copies their variant and quiet look, so it ships in the same PR as the desktop pill. Nothing waits on an npm publish.
- The floating action button considered earlier is dropped. The DS still has no FAB, and none is needed now.
- Using `<Button variant="ghost" size="md" icon>` gives the phone Decisions header button the DS focus ring and tap-highlight, matching every existing header button.

### 8.4 Possible later contribution (unchanged, not blocking)

`DecisionCard` is close to `ConfirmChip` in `@protolabsai/ui/ai`: an inline, non-modal "the agent proposes, the person decides" control. If a second surface needs it (for example the HITL form), contribute a `ChoicePrompt` to `@protolabsai/ui/ai`:

- Props: `title`, `children` (the question), `options: {id, label, description?, recommended?}[]`, `onChoose(id)`, `status: "open" | "sending" | "done" | "closed"`, `footer?`.
- It would own the same invariants as `ConfirmChip`: a one-shot latch per decision id, never stealing focus, and DOM order question → options.

## 9. Out of scope

- Creating a decision from the console. Only agents raise them.
- Free-text answers inside the card. "Reply in chat instead" covers that.
- Webhook, Discord and Telegram channels. They are backend config with no console UI.
- A history view of past decisions. The source chat and the Activity feed keep that record.
- Dismissing or hiding the header button while decisions are pending. It goes away when nothing is waiting.
- A floating button over the composer (withdrawn in revision 1).

## 10. Open questions

### Answered or retired by revision 1

- **Q2 (mount point)** — retired. The entry point is now the utility-bar pill (≥ 768 px) and the `MobileShell` header button (< 768 px), not the desktop top bar.
- **Q10 (placement, for Josh)** — answered by revision 1.

### For the coder and Claude Code, against the codebase

1. **Push event.** *Answered (protoEngineer, from bd-00u8k):* the server pushes a `decision.changed` bus event, the same pattern as `directive.changed` and `useDirectives` (#4861). Still open: does the hub receive other fleet members' `decision.changed` events, or does it fall back to `FleetTurnWatch`'s 5 s poll for them?
3. **Offline answers.** Can the backend store an answer for a stopped agent and resume the parked turn when it starts? This decides §5.7.
4. **Consequential options.** Should the decision API let an agent mark an option as needing a second confirm (for example "Delete the branch")? This spec says no for the first version.
5. **Answer payload.** *Answered (protoEngineer, from bd-00u8k):* `POST /api/decisions/{id}/answer` with `{option_id}` or `{text}`. Still open: the server should hand the resumed turn the option's label text, so the agent reads what Josh saw.
6. **Default action.** What happens on expiry when the agent gave no default? §4.2 assumes "carry on without this step"; the backend must choose.
7. **HITL overlap.** An attended session already shows a HITL form and calls `notifyIfHidden` (`ChatSurface.tsx`). Should an attended blocking `ask_human` also appear in the Decisions panel?
8. **Hub attention list.** Does the fleet hub have an attention list today, or is the Decisions panel that list?
9. **Toast with action.** Does `useToast` support an action button? (The Knowledge undo toast puts a `Button` in `message`, so probably yes.) If so, a new decision while the window is visible could show a neutral "protoEngineer needs a decision · View" toast.
11. **Controlled `UtilityWidget`.** `UtilityWidget` keeps its open state internally. A notification click and "Open the panel" from an Activity event need to open it from outside. Add optional `open` / `onOpenChange` props, keeping today's uncontrolled behaviour as the default?
12. **`aria-haspopup` on the pill.** `UtilityWidget`'s pill doesn't expose `aria-haspopup="dialog"` or `aria-expanded`. Add both to `UtilityWidget` for every widget, not only Decisions?
13. **Pushed-view spacer.** On pushed views the header's right slot is an `aria-hidden` spacer that centres the title. Is it safe for `MobileShell` to render the phone Decisions header button there instead while a decision is pending, or do any mobile e2e specs pin that spacer?
14. **Feed badge.** The feed raises its badge on `inbox.item`. If a parked decision also arrives as an inbox item, the cluster would show two counts for one decision. Can the feed skip decision events for its unread count?
15. **Two sheets.** The session sheet is already a bottom `Drawer` opened from the header. Opening the Decisions sheet while the session sheet is open (for example from a notification) must close the session sheet first, so two bottom sheets never stack. Is there one UI-store owner for "which mobile sheet is open"?
23. **Raw button or DS `Button` — resolved.** The existing header buttons are DS `<Button variant="ghost" size="md" icon>` since #551, not raw `<button className="mshell-head-btn">`. The phone Decisions header button uses the same variant to match them. No choice remains.
24. **Header focus ring — resolved.** The #551 DS Button swap gave every existing header button the DS focus ring. The phone Decisions header button inherits it by using `<Button variant="ghost" size="md" icon>`. No separate a11y card is needed for the button this spec adds.

### For Josh

16. **Phone icon.** Icon and count only, beside New chat, per your revision. Is `MessageCircleQuestion` (a speech bubble with a question mark) clear enough on its own, given a phone has no hover? The accessible name says "Decisions, N waiting on you".
17. **Quiet pill on desktop.** This spec keeps the pill rendered, without a badge, when nothing is pending, so the widgets to its right never shift. Is a permanent quiet icon in the cluster acceptable, or would you rather it hide and accept the shift?
18. **Slot.** Before Background jobs (this spec: a blocking item outranks reports), or after the feed?
19. **Pushed views.** This spec shows the header button on pushed views too (in the spacer's slot), so a decision is reachable from every screen. Is that right, or only on the chat root?
20. **One tap** (kept from the first draft).  Is sending on one tap right, with no confirm, given agents write each option to be safe to pick?
21. **Recommended option** (kept from the first draft). Should agents be allowed to mark one option "suggested"? It helps a phone answer, but it nudges.
22. **Expired items** (kept from the first draft). Is it right that an expired decision drops out of the count silently when the panel is closed, with the record kept in the chat and the Activity feed?
