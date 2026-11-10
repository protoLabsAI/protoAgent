# Settings → Storage — design spec

- Surface: protoAgent console, Settings → "This console" → **Storage** (`apps/web/src/settings/`)
- Tracking: protoLabsAI/protoAgent#4981 (epic), ADR 0114 D6
- Requested by: Josh, via his Claude Code session, 2026-11-09 (`inbox/settings-storage/BRIEF.md`)
- Status: **final. Approved by Josh on 2026-11-09** ("looks good to me"), with the decisions in §9 applied. Next steps: this spec goes into protoAgent's `docs/design`, and the build follows as a draft PR for Josh to test.
- Preview: pinned console artifact `a-1794267950798-f4cb9a` ("Settings → Storage preview"), version 17 or later. Five states: normal, freed, full, normal on plain http, and Keep data refused.
- Uses: `@protolabsai/ui` and `@protolabsai/design` only. No new component, token or accent colour (see §8).
- Data: the sample profile in the brief (10 agents, 2.88 MB of chats, 70 KB quick chats, 45 KB layout and preferences, browser estimate 3.4 MB of about 60 GB).

## 1. What the page answers

In order of importance:

1. **Is my chat history being saved?** One line at the top: yes or no.
2. **What's using the space?** One row per agent, plus quick chats and layout and preferences.
3. **Clear the old stuff.** Clear one agent, quick chats, or every agent that left the fleet. This browser's copy only.
4. **Keep my data.** Ask the browser not to clear the console's data on its own.
5. **What did it clear automatically?** The last automatic clear: when and what.

The common case is "everything's fine". In that case the page is a green dot, one sentence and a list. It shows no warning, no meter and no call to action. Information never renders as a problem. The only state with a coloured surface is "full", because then something really isn't working.

## 2. Placement

- Add `{ id: "storage", label: "Storage", icon: "HardDrive" }` to `CONSOLE_SECTIONS` in `sections.ts`, after `keybindings`. Update the `sections.test.ts` pin and the icon map in `sectionIcons`. `HardDrive` must resolve in lucide's `icons` map, which the test checks.
- The panel uses `SettingsSubPanel` like `KeybindingsPanel`: `title="Storage"`, `kicker="what this browser keeps for this console — saved on this device"`. The kicker matches the Chat and Keyboard pattern.
- No header actions. Every action sits next to what it affects.

## 3. Layout

From top to bottom, inside `stage-body`:

```
Storage
what this browser keeps for this console — saved on this device

● Chats are saving in this browser.
  3.4 MB used, with plenty of room left.

What's using space
Clearing removes this browser's copy only. The server keeps the last 24 hours
of each chat, and your other devices aren't affected.
2 agents are no longer in your fleet (302 KB).                  [Clear them]
┌──────────────────────────────────────────────────────────────────┐
│ protoEngineer  [this window]             2 chats · 610 KB   Clear │
│ warBoss                                  5 chats · 450 KB   Clear │
│ cliffNotes necroMunda                    6 chats · 370 KB   Clear │
│ …                                                                 │
│ threeDee  [not in fleet]                 4 chats · 175 KB   Clear │
│ Quick chats                            4 threads · 70 KB    Clear │
│ Layout and preferences                             45 KB          │
│   Includes your sign-in, so it isn't cleared here.                │
└──────────────────────────────────────────────────────────────────┘

Keep data                                                [Keep data]
Asks the browser not to clear this console's data on its own, for
example when your disk is low.

Last automatic clear
Nothing has been cleared automatically.

▸ Technical details
```

### 3.1 Status line

One block at the top. It reads the D6 pressure signal (`ok | evicted | failing`) first, then the browser estimate. There is no "nearly full" state and no new status value: automatic clearing handles a filling browser, and `failing` covers the case where it couldn't.

| Condition | Treatment | Heading | Body |
|---|---|---|---|
| `ok`, estimate available | `Dot success` | Chats are saving in this browser. | "3.4 MB used, with plenty of room left." |
| `ok`, estimate unavailable | `Dot success` | Chats are saving in this browser. | "About 3.0 MB saved." (the console's own sum) |
| `evicted`, or a recorded automatic clear in the last 24 hours | `Dot success` | Chats are saving in this browser. | "3.1 MB used, with plenty of room left. Made room today at 14:12 by clearing 5 old chats." |
| `failing` | `Callout warning`, no callout title | New messages aren't being saved in this browser. | "Chats keep working, and the server keeps the last 24 hours of each one. Free up space to start saving again." Primary button **Free up space**. |

Rules:

- No meter. At 3.4 MB of 60 GB a bar is an empty line that tells the operator nothing, and the page never warns before `failing`.
- "Freed" is not a problem state. It keeps the green dot and adds one sentence. The details live under "Last automatic clear".
- The `failing` callout uses warning, not error. Private windows hit it legitimately (D6), and chat keeps working.
- **Don't use `Callout`'s `title` for the sentence.** The DS styles `.pl-callout__title` as a lowercase mono kicker, so a sentence there renders as "new messages aren't being saved in this browser". Put the sentence in the callout body as the first line, in sans at `--pl-font-size-base` and `--pl-font-weight-medium`, the same as the `ok` heading. The preview does this.

### 3.2 What's using space

- Intro sentence (always shown): "Clearing removes this browser's copy only. The server keeps the last 24 hours of each chat, and your other devices aren't affected."
- Rows are sorted by size, largest first. The agent you're on is not pinned to the top. It carries a neutral `this window` badge.
- Every agent row shows the agent's **real display name** from the fleet list, including the host. There is no "This instance (host)" label. When the window is on the host, the host's row carries `this window`. (The preview uses `protoAgent` as a stand-in for the host's name, because the sample doesn't give it.)
- Agent row: display name, then the parent name for a nested agent (`cliffNotes necroMunda`) in muted text, then badges, then `N chats · size` in tabular figures, then a ghost `sm` **Clear** button with `aria-label="Clear <name>'s chats"`.
- **Last saved** goes in a tooltip only, on the row's `N chats · size` text: "Last saved 3 days ago". Use the DS `Tooltip`, which also shows on keyboard focus. It is not in the row text, so it costs no width on phone. The preview doesn't show it, because the sample has no dates.
- An agent that left the fleet keeps its last known name and gets a neutral `not in fleet` badge. It isn't styled as an error.
- When agents have left the fleet and the page isn't in the `failing` state, one line above the list says "2 agents are no longer in your fleet (302 KB)." with a **Clear them** button. In `failing`, Free up space covers them.
- **Quick chats** is one row for the palette and direct-message threads across all agents: `4 threads · 70 KB`, Clear. This is the only category with a Clear button.
- **Layout and preferences** shows its size and the note "Includes your sign-in, so it isn't cleared here." It has no Clear button and no reset. Sign-in and dismissals are never offered, and there is no "Reset layout" on this page. Unregistered keys (plugins, design system) are counted here too.

### 3.3 Clearing the agent you're on

- Allowed when no reply is running. It works like automatic clearing: chats open in a tab are kept, and every other saved chat of that agent goes.
- While a reply is running, its Clear button is disabled and a note under the row says "Replying now. You can clear this after the reply finishes." The row stays visible so the numbers still add up. When the reply ends, the button enables again without a reload.

### 3.4 Keep data

A `setting-row`, matching Chat's rows. The control is a button, not a `Switch`: the page can't take the browser's grant back, so a switch would offer an "off" it can't honour.

| Situation | Control | Description |
|---|---|---|
| Secure origin, not yet asked | `Button sm` **Keep data** | Asks the browser not to clear this console's data on its own, for example when your disk is low. |
| Granted | `Dot success` + "On" | The browser won't clear this console's data on its own. The console still clears old chats itself when it runs out of room. |
| Refused by the browser (no prompt shown) | `Button sm` **Keep data**, enabled | This browser chose not to keep it this time. That's the browser's call and nothing is wrong: your chats save as usual. Browsers often say yes later, for example once you've used the console for a while or bookmarked it. |
| Plain http (not a secure context) | `Button sm` **Keep data**, disabled | Only works on a secure (https) address or in the desktop app. This console is on plain http. |

The refused state is plain text in muted colour, with no warning colour and no icon. Pressing the button again simply asks again.

### 3.5 Last automatic clear

- Heading "Last automatic clear", one sentence: "Today at 14:12: 5 chats from threeDee (4 chats) and promptInjection (1 chat), which are no longer in your fleet. 302 KB freed."
- No record: "Nothing has been cleared automatically."
- **Storage:** the record lives in its own small localStorage key in the `ephemeral` category of the key registry (`apps/web/src/lib/storage.ts`). It holds the time, the chat count, the bytes freed and the agents or categories cleared. It survives a reload.
- **Manual clears never write to it.** Clearing from this page confirms with a toast and updates the list, but "Last automatic clear" keeps showing the last automatic one.

### 3.6 Technical details

A native `<details>`, closed by default, with the summary "Technical details". This is the only place jargon may appear:

- Browser's estimate: 3.4 MB used of about 60 GB, including about 400 KB of browser overhead. Or "unavailable (needs a secure address and Safari 17 or later)".
- Chats: about 3.0 MB in the `protoagent-transcripts` IndexedDB database.
- Layout, preferences, dismissals and sign-in: about 45 KB in localStorage.
- Persistent storage: granted, requested and not granted, not requested, or not available on this address. Saving: ok or failing.

The overhead line explains why the browser's number is bigger than the rows' sum (3.4 MB against about 3.0 MB in the sample).

## 4. Clearing

Every Clear opens `ConfirmDialog` with `destructive`. Each dialog says what goes and what stays.

| Action | Title | Body | Confirm |
|---|---|---|---|
| One agent | Clear protoEngineer's chats from this browser? | Removes 2 saved chats (610 KB) from this browser. Stays: the server's copy of the last 24 hours of each chat, protoEngineer's memory and settings, and anything on your other devices. Chats open in a tab stay too. | Clear chats |
| Left the fleet | Clear agents no longer in your fleet? | Removes 5 saved chats (302 KB) from threeDee and promptInjection. Stays: the server's copy of the last 24 hours of each chat, every agent still in your fleet, and anything on your other devices. Chats open in a tab stay too. | Clear chats |
| Quick chats | Clear quick chats from this browser? | Removes 4 quick chat threads (70 KB) from the command palette and direct messages. Stays: the server's copy of the last 24 hours of each chat, every agent's regular chats, and anything on your other devices. Chats open in a tab stay too. | Clear quick chats |
| Free up space (full) | Free up space? | Clears old chats from this browser until new messages fit, in this order: 1. agents no longer in your fleet, 2. quick chats from other windows, 3. agents you haven't opened in a day, 4. protoEngineer's oldest chats. Never cleared: chats open in a tab, and protoEngineer's reply in progress. The server keeps the last 24 hours of each chat. | Free up space |

**Free up space** runs the same eviction as the automatic clear, so it does write the "Last automatic clear" record. It's the automatic routine started by hand.

After a clear:

- A neutral toast: "Cleared protoEngineer's chats from this browser. 610 KB freed." For Free up space: "Made room: cleared 9 old chats (372 KB). New messages are saving again."
- The row leaves the list and the totals update. If the clear fixed a `failing` state, the status line returns to its `ok` form.
- Focus moves to the next row's Clear button, or to the "What's using space" heading when the list is empty. It must never fall to `<body>`.

## 5. States in the preview

| State | What it shows |
|---|---|
| Normal | The sample profile. Green dot, "3.4 MB used, with plenty of room left." |
| Freed | threeDee and promptInjection gone, "Made room today at 14:12 by clearing 5 old chats." and the last automatic clear filled in. |
| Full | `failing`, with protoEngineer mid-reply: warning callout, **Free up space**, protoEngineer's Clear disabled with its note. |
| Plain http | No browser estimate ("About 3.0 MB saved."), Keep data disabled with its reason. |
| Keep data refused | The refused copy from §3.4, button still enabled. |

Every Clear button, Free up space and Keep data work in the preview, so the dialogs and toasts can be reviewed.

## 6. Phone (about 380 px)

- Settings opens as a sheet, so there is no rail. The panel is one column.
- Rows wrap: the name and badges take the first line, and `N chats · size` and Clear drop to a second line, aligned right.
- Buttons in rows keep at least a 32 px target (the `sm` button plus row padding). On touch the row is 44 px tall.
- The Last saved tooltip opens on tap of the size text.
- Dialogs use the default `Dialog` width, which already caps at the viewport.

## 7. Accessibility

- Status line: the heading text carries the meaning. The dot is `aria-hidden`, so colour is never the only signal (WCAG 1.4.1).
- When the page moves into or out of `failing` while open, the change is announced through a polite live region. The callout is not `role="alert"` on first render.
- Clear buttons have a unique accessible name per row ("Clear warBoss's chats"). A disabled Clear is paired with visible text that says why, not only a `title`.
- The Last saved tooltip opens on hover and on keyboard focus, and the text it adds is also in the row's accessible description.
- `ConfirmDialog` traps focus, closes on Escape, and returns focus to the button that opened it, or to the next row if that row was removed.
- Section headings are real `h3` elements under the panel's `h2`, so screen reader users can jump between them.
- Contrast: only DS text tokens (`fg`, `fg-muted`) and DS status surfaces are used. No new pairs need checking.

## 8. Design-system fit

Components used, all existing: `SettingsSubPanel` / `PanelHeader`, `Badge` (neutral), `Dot` (success), `Callout` (warning, no title), `Button` (ghost sm, sm, primary), `Tooltip`, `ConfirmDialog`, and toasts through `useToast`. Layout uses the console's existing `setting-row` / `setting-meta` / `setting-desc` classes.

No new tokens, components or accent colours. Two small layout rules go in the panel's own CSS: the row layout and the wrapping rule at phone width. Both use `--pl-space-*` only.

DS note: `Callout` has no sentence-case title. Its `title` is a lowercase mono kicker by design. This page works around it (§3.1). If more surfaces need a sentence heading inside a callout, that's a DS change to raise separately, not part of this build.

## 9. Decisions

Josh accepted these on 2026-11-09.

1. **No "nearly full" state.** The full state plus automatic clearing cover the real case. No `near` value is added to the D6 signal, and the page has no meter.
2. Information never renders as a problem (follows from 1).
3. **Clear covers quick chats only** among the categories. No "Reset layout" on this page.
4. **The agent you're on can be cleared** when no reply is running, keeping any chat open in a tab, the same as automatic clearing. While a reply runs, Clear is disabled with the reason.
5. **The host shows its real agent name**, with the `this window` badge when it's the one you're on. No "This instance (host)".
6. **Keep data stays a button.** The refused copy says it's the browser's choice and nothing is wrong (§3.4).
7. **The last automatic clear has its own small key** in the `ephemeral` category, separate from manual clears (§3.5).
8. **Last saved goes in a tooltip only** (§3.2).
9. **ADR 0114 D6 gets amended at build time.** Its Settings → Storage list includes "clearing another backend's database". There is one database now (`protoagent-transcripts`), so that item is dropped. The same amendment should note the decisions above that change D6's page description: no meter or nearly-full state, and the per-row Clear limited to agents and quick chats.
