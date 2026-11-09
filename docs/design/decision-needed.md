# "Decision needed" console item — design spec

- Surface: core console, `apps/web` (issue #4725)
- Requested by: Josh, 2026-11-08
- Status: draft, pending Josh's approval. The server side is board card bd-00u8k. The console card (bd-bqyfs) is parked and gets re-specced against this doc.
- Uses: `@protolabsai/ui` and `@protolabsai/design` only. No new design-system component is needed for the first version (see §8).

## 1. What this item answers

The item answers one question from anywhere in the console: **is an agent waiting on me, and what does it need?**

It lets Josh do two things:

1. Answer in one action, by picking one of the options the agent offered.
2. Open the chat the decision came from, if he wants the context first.

A pending decision is a **request**, not an alarm. Nothing in this spec uses warning or error colour for a decision that is simply waiting.

## 2. Recommendation: where a decision lives

**Recommended: a top-bar indicator, backed by a desktop notification.** Each channel has one job:

| Channel | Job | When |
|---|---|---|
| **Top-bar "Decisions" button** (primary) | The always-visible place to see and answer every pending decision, from every fleet member. | Shown while at least one decision is pending, on every surface, at 1440 px and 390 px. |
| **Desktop notification** (secondary) | Reach Josh when the console window is hidden or in the background. | Once per new decision, through the existing `notifyIfHidden` (`apps/web/src/lib/notify.ts`). |
| **The decision in its own chat** (context) | Show the decision where the agent stopped, so reading the chat explains itself. | Always, as the last item of the parked turn. |

### Why not the other places

- **Rail badge.** The `SurfaceRail` belongs to one agent's surfaces, and it is replaced by `MobileNav` on a phone. A decision from protoEngineer would be invisible while Josh is looking at another agent. The badge also caps at "9+" and carries no words.
- **A composer strip, like `BackgroundWorkStrip` or the directive-ledger strip.** Both are scoped to one chat. A decision must be seen from every chat and every agent, so it can't live in one chat's composer dock.
- **The utility-bar Activity feed.** It is a read-only history feed, and the bottom bar is far from where the eye rests. A decision can appear in the feed as an event (see §5.9), but the feed is not where Josh answers it.
- **A `Banner` under the top bar.** It is too loud for a request, and two or three agents would stack two or three banners.

### Placement detail

- The button sits in the top bar's right cluster, **directly before the hamburger** (`.app-topbar-menu` in `App.tsx`). It composes `Button variant="ghost" size="sm"` with a `Count`.
- It is **not rendered** when nothing is pending. The top bar never shows an empty or "0" state.
- It is the same on every surface, because the top bar is shared chrome.
- The hub sees every fleet member, so the count is fleet-wide. A standalone agent window shows only its own decisions.

## 3. Information architecture

There are three levels.

1. **Indicator** (top bar). It answers "is anything waiting, and how many?"
2. **Decisions panel.** It lists every pending decision as a card, each answerable in place.
   - Desktop: a `Popover` anchored to the indicator, `side="bottom"`, `align="end"`, 400 px wide, body scrolls past 70vh (`ScrollArea`).
   - Phone (below the console's 768 px `mobileBreakpoint`): `Drawer side="bottom"`, up to the component's 85dvh cap.
   - Title: **Decisions waiting on you**.
3. **Decision card.** One decision: who asked, the question, the options, what happens if nobody answers, and a link to the chat. The same card renders in the panel and in the source chat.

### 3.1 Order in the panel

1. Decisions with an expiry, soonest expiry first.
2. Then decisions without an expiry, oldest first.

Cards from different agents are not grouped under agent headings. Each card names its agent, and a flat list keeps "what's most urgent" at the top.

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

### 4.1 Indicator

| Case | Visible | Accessible name |
|---|---|---|
| 1 pending, desktop | **Decision** `1` | "1 decision waiting on you" |
| N pending, desktop | **Decisions** `N` | "N decisions waiting on you" |
| Phone | Icon (`MessageCircleQuestion`, 16 px) and `N` | same as above |
| Tooltip | "Decisions waiting on you" | — |

### 4.2 Panel

| Element | Text |
|---|---|
| Title | Decisions waiting on you |
| Intro (one line) | Your agents stopped to ask you these. Pick an answer and the agent carries on. |
| Card meta | {Agent} · asked {when} |
| If nobody answers | If nobody answers by {time}, {Agent} will {default action}. |
| Open chat link | Open chat: {chat title} |
| Long question toggle | Show full question / Show less |
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

Clicking it brings the console forward and opens the Decisions panel. One notification per decision, never repeated by a poll or a reload.

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

- Indicator: not rendered.
- Panel: can't be opened from the top bar. If it is open when the last decision clears, it shows the "That's everything" line for the rest of that view.
- No notification, no empty badge.

### 5.2 Loading

- The indicator stays hidden until the first load finishes, so it never flashes "0".
- The panel, if opened from a notification before the load finishes, shows two `Skeleton lines={3}` blocks.

### 5.3 One pending

- Indicator: **Decision** `1`.
- Panel: one card, fully expanded.
- Source chat: the same card is the last item of the parked turn.

### 5.4 Several, from different agents

- Indicator: **Decisions** `3`.
- Panel: cards ordered per §3.1, with a `Divider` between them. Each card names its own agent. For example: protoEngineer (release, expires 15:45), Cindi (blog post publish time, no expiry), designSystem (accept a contrast exception, no expiry).
- Answering one card never moves focus out of the panel, and the others stay put.

### 5.5 Answered

1. Josh picks an option. The button shows `loading`.
2. On success, the card shows the "Sent" line (§4.4) and a `StatusDot status="success"`. This is the only place success colour appears, and the words carry the meaning.
3. After 4 seconds the card leaves. Focus moves to the next card's question, or to the panel title if none are left. The count updates.
4. In the source chat, the card stays in the transcript, frozen, with "You chose "{option}" · 14:52". The agent's resumed turn follows it.

### 5.6 Expired, or no longer needed

- The backend closes the decision (expiry, or the agent resolved it another way).
- If the panel is closed, the decision drops out of the count silently. Nothing announces it.
- If the panel is open, the card swaps its options for the neutral callout from §4.4 and leaves when the panel closes. It never vanishes under Josh's pointer.
- In the source chat, the card freezes with the same sentence.

### 5.7 Agent offline

- The card shows `StatusDot status="neutral" label="offline"` after the agent name.
- The options stay enabled **if** the backend holds the answer until the agent restarts (Q3). The card adds: "{Agent} is offline. It gets your answer when it starts again." Sent copy becomes "Saved: "{option}". {Agent} gets it when it starts again."
- If the backend can't hold answers, the options are disabled and the line reads: "{Agent} is offline. Start it to answer." with a ghost **Start** button, matching the existing stopped-agent banner action.
- Offline is never shown as an error or in warning colour.

### 5.8 Couldn't send

- The `Alert status="warning"` from §4.4 appears on the card, above the options. Focus moves to it.
- The options stay enabled so Josh can retry or pick another one.
- A 409 is not a failure: it uses the info callout.

### 5.9 Other surfaces

- **Activity feed.** "protoEngineer asked for a decision" and "You answered protoEngineer's decision" appear as ordinary feed events. They link to the panel. They are not answerable there.
- **Fleet hub attention list.** Uses the same card, if the hub has an attention list (Q8).

### 5.10 Themes and sizes

- Light and dark use tokens only: no literal colours, spacing values or radii.
- Question text uses `--pl-color-fg`. Meta, option explanations and the "if nobody answers" line use `--pl-color-fg-muted`, which measures 7.24:1 (dark) and 7.73:1 (light) on `--pl-color-bg-raised`, as measured for the directive ledger. Do not use `--pl-color-fg-subtle` for any text on the card: it fails at 3.42:1 on the light surface.
- The `Count` on the indicator uses the DS default pill. It is not tinted warning.
- 1440 px: popover, 400 px wide.
- 390 px: bottom sheet. Option buttons are full width at `size="md"`, well above the 24 px target minimum (WCAG 2.5.8). The top-bar indicator is icon plus count, with at least a 24 px target.

## 6. Accessibility

- **Indicator.** A real `Button` with the accessible name from §4.1. It has `aria-haspopup="dialog"` and `aria-expanded`.
- **Live announcement.** One app-level `role="status"` region announces each new decision once, politely: "protoEngineer needs a decision." It is never `role="alert"`; a request does not interrupt (WCAG 4.1.3).
- **Panel.** `Popover` and `Drawer` provide focus management, Escape to close and focus return to the indicator. The dialog name is the title.
- **Card.** Each card is an `article` labelled by "{Agent}: {first words of the question}". The options are a `role="group"` labelled "Answer". Option explanations are linked through `aria-describedby`.
- **Results.** "Sent" and the callouts are inside a polite live region on the card. Focus moves as in §5.5 and §5.8.
- **Keyboard.** Every control is a DS `Button` or `TextLink`. Nothing is hover-only; the tooltips repeat visible information.
- **Motion.** The 4-second "Sent" hold has no animation. It inherits the overlay reduced-motion handling.
- **Colour.** No state relies on colour: every state has a sentence.
- **Notification permission.** The console asks for it only the first time a decision arrives, through `notifyIfHidden`. Refusing it changes nothing else.

## 7. Behaviour rules for the build

- **One action.** A tap on an option sends it. There is no confirm step. The agent writes options so that each is safe to pick (Q4 asks whether some options need a confirm).
- **No duplicates.** A decision is keyed by its id. The panel, the source chat and the notification all read one store.
- **Freshness.** The indicator updates from the `decision.changed` bus event, not a slow poll (Q1, answered). A decision that waits 5 minutes before Josh can see it repeats the incident.
- **Closed means closed.** Once the server says a decision is answered, expired or withdrawn, no control on any surface can answer it.

## 8. Components

### 8.1 Design-system components used (all exist today)

| Component | Import | Use |
|---|---|---|
| `Button` | `@protolabsai/ui/primitives` | Indicator (ghost, sm), options (default or primary, md), Reply in chat instead (ghost), Try again, Start, Show full question |
| `Count` | `@protolabsai/ui/primitives` | The number on the indicator |
| `Badge` | `@protolabsai/ui/primitives` | "suggested" on the recommended option |
| `Avatar` | `@protolabsai/ui/primitives` | The agent on each card |
| `Callout` | `@protolabsai/ui/primitives` | Answered elsewhere (info), no longer needed and expired (neutral) |
| `Divider` | `@protolabsai/ui/primitives` | Between cards |
| `TextLink` | `@protolabsai/ui/primitives` | Open chat |
| `StatusDot` | `@protolabsai/ui/data` | Sent (success), offline (neutral) |
| `Alert` | `@protolabsai/ui/data` | Couldn't send |
| `Skeleton` | `@protolabsai/ui/data` | Loading |
| `ScrollArea` | `@protolabsai/ui/data` | Panel body |
| `Popover` | `@protolabsai/ui/overlays` | Panel on desktop |
| `Drawer` | `@protolabsai/ui/overlays` | Panel on phone (bottom) |
| `Tooltip` | `@protolabsai/ui/overlays` | Indicator label, full timestamps |
| `useToast` | `@protolabsai/ui/overlays` | "Sent" when the panel has closed |
| `Header` (`actions` slot) or `.app-topbar` | `@protolabsai/ui/app-shell` | Where the indicator mounts |

### 8.2 Console composites (in `apps/web`, not the design system)

| Composite | Purpose |
|---|---|
| `DecisionsIndicator` | The top-bar button and count. Renders nothing when the count is 0. |
| `DecisionsPanel` | Popover or bottom Drawer by breakpoint; ordering; the "That's everything" line. |
| `DecisionCard` | One decision and every state in §5. Used in the panel and in the source chat transcript. |
| `useDecisions()` | Fleet-wide store: load, `decision.changed` push updates, answer, 409 handling, `notifyIfHidden(title, body?)` once per id. |

The composites use tokens only and pass `ds_check`.

### 8.3 Possible design-system contribution (follow-up, not blocking today)

`DecisionCard` is close to `ConfirmChip` in `@protolabsai/ui/ai`: an inline, non-modal "the agent proposes, the person decides" control. If a second surface needs it (for example the HITL form), contribute a `ChoicePrompt` to `@protolabsai/ui/ai`:

- Props: `title`, `children` (the question), `options: {id, label, description?, recommended?}[]`, `onChoose(id)`, `status: "open" | "sending" | "done" | "closed"`, `footer?`.
- It would own the same invariants as `ConfirmChip`: a one-shot latch per decision id, never stealing focus, and DOM order question → options.

This gets filed as a DS issue once the console version settles. It is not needed to ship today.

## 9. Out of scope

- Creating a decision from the console. Only agents raise them.
- Free-text answers inside the card. "Reply in chat instead" covers that.
- Webhook, Discord and Telegram channels. They are backend config with no console UI.
- A history view of past decisions. The source chat and the Activity feed keep that record.

## 10. Open questions

### For the coder and Claude Code, against the codebase

1. **Push event.** *Answered (protoEngineer, from bd-00u8k):* the server pushes a `decision.changed` bus event, the same pattern as `directive.changed` and `useDirectives` (#4861). `useDecisions()` follows `useDirectives`. Still open: does the hub receive other fleet members' `decision.changed` events, or does it fall back to `FleetTurnWatch`'s 5 s poll for them?
2. **Mount point.** Should the indicator go in `Header`'s `actions` slot, or in `.app-topbar` next to `.app-topbar-menu`, given `Header` is currently passed only `name` and `org`?
3. **Offline answers.** Can the backend store an answer for a stopped agent and resume the parked turn when it starts? This decides §5.7.
4. **Consequential options.** Should the decision API let an agent mark an option as needing a second confirm (for example "Delete the branch")? This spec says no for the first version.
5. **Answer payload.** *Answered (protoEngineer, from bd-00u8k):* `POST /api/decisions/{id}/answer` with `{option_id}` or `{text}`. Options send `{option_id}`. Still open: the server should hand the resumed turn the option's label text, so the agent reads what Josh saw. `{text}` is not used by the card in this version; free text goes through "Reply in chat instead".
6. **Default action.** What happens on expiry when the agent gave no default? §4.2 assumes "carry on without this step"; the backend must choose.
7. **HITL overlap.** An attended session already shows a HITL form and calls `notifyIfHidden` (`ChatSurface.tsx`). Should an attended blocking `ask_human` also appear in the Decisions panel, so there is one place for everything waiting on Josh?
8. **Hub attention list.** Does the fleet hub have an attention list today, or is the Decisions panel that list?
9. **Toast with action.** Does `useToast` support an action button? If so, a new decision while the window is visible could show a neutral "protoEngineer needs a decision · View" toast. If not, the indicator and the live announcement are enough.

### For Josh

10. **Placement.** Is the top-bar indicator plus a desktop notification the right pairing, rather than a rail badge or the Activity feed?
11. **One tap.** Is sending on one tap right, with no confirm, given agents write each option to be safe to pick?
12. **Recommended option.** Should agents be allowed to mark one option "suggested"? It helps a phone answer, but it nudges.
13. **Expired items.** Is it right that an expired decision drops out of the count silently when the panel is closed, with the record kept in the chat and the Activity feed?
