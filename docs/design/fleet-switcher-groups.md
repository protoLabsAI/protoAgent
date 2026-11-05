# Fleet switcher: groups, filter, pins — spec

Status: **APPROVED, ready to build** (Josh, 2026-11-03; see Decisions log 6–10). Owner: designSystem. Requested by Josh, 2026-11-03.
Surface: `apps/web/src/app/FleetSwitcher.tsx` (protoAgent console, top-left agent dropdown).

## Problem

The switcher is one flat list in roster order (18 agents today): StatusDot, name, a
"this instance" Badge on the host, a Check on the current agent, then "New agent" and
"Fleet settings". Local, remote and test agents are mixed together, so finding one means
reading the whole list.

## Building blocks (all existing DS exports)

| Need | Component |
|---|---|
| Dropdown shell, focus trap, outside-click | `Menu` (`@protolabsai/ui/menu`) |
| Agent row | `MenuItem` with `icon` = `StatusDot`, trailing `action` = pin star |
| Section titles | `MenuLabel` |
| Section breaks | `MenuSeparator` |
| "Move to group…" list | `MenuSub` |
| "this instance", machine name, counts | `Badge status="neutral"` |
| Filter box | `Input` (`@protolabsai/ui/forms`), clearable variant |

Two gaps, flagged rather than invented: the DS has **no collapsible menu section header**
(`MenuLabel` isn't interactive) and **no pattern for a text field inside a `Menu`**
(Radix typeahead captures typing). See "Gaps" at the end.

## Layout and section order

Top to bottom. A section with no rows is hidden, along with its title and separator.

1. **Fixed header row**: filter box (at 10 or more agents), then icon buttons
   **New agent** and **Fleet settings**, with matching tooltips and aria-labels.
   Both retain the existing `fleetSettingsDisabledReason` gating and disabled tooltip.
   Desktop uses a sticky opaque header inside the DS Menu; only the rows scroll.
2. **Current**: the agent this window is on, with StatusDot, name and Check.
3. **Pinned**: other starred agents, in the order they were pinned.
4. **Ungrouped**: remaining local agents with no group, without a title.
5. **Operator groups**: remaining agents in their own group, in Fleet settings order.
6. **Remote**: remaining remote agents, with a machine Badge on each row.

**Each agent once (Josh, 2026-11-04).** Current takes precedence over Pinned, which
takes precedence over the agent's home section. Empty sections are hidden. There is
no Recent section and no footer.

**Row anatomy** (every section): StatusDot (`success` + pulse when running, else
`neutral`), display name (`label ?? name`), optional Badges ("this instance" on the host,
machine name in Remote), Check on the current agent, trailing star action. The "this
instance" agent is ungrouped by default but can be assigned to a group like any other.

## Filter

- Shown when the fleet has **10 or more agents**. It sits at the top and has focus when the
  menu opens, so the operator can just start typing.
- Matches a case-insensitive substring of the display name, the handle (`name`), the group
  name and, for remote agents, the machine name.
- While the filter has text, matching rows retain the same Current → Pinned → home
  precedence, and **collapsed groups open temporarily** so their matches show.
  A section with no matches is hidden. Each agent appears once.
- The first match is highlighted. **Enter** switches to it. **Escape** clears the text
  first, and closes the menu if the box is already empty.
- Clearing the box puts the collapsed state back as it was. The filter text is never saved.

## Collapsed groups

- Every titled section (operator groups and Remote) has a header row with a chevron:
  pointing right when collapsed, down when open. Clicking the header or pressing
  Enter or Space on it toggles the group. Toggling never closes the menu.
- A collapsed header still shows the group's count Badge, and a small `StatusDot`
  if any agent in it is running, so a collapsed group isn't silent.
- If the current agent's group is collapsed, the group stays collapsed (the current agent
  is already at the top).
- Collapsed state is remembered per browser (see Data).

## Empty states

| Case | What shows |
|---|---|
| Only the host agent | Header actions and Current row. No section titles, no filter. |
| No pins | Those sections are hidden. No placeholder. |
| An operator group with no agents | Hidden in the switcher. Shown in Fleet settings as "No agents yet". |
| No paired machines | No Remote section. |
| Filter matches nothing | One disabled row: "No agents match "{text}"". |
| A remote machine is unreachable | Its agents stay listed with a `neutral` StatusDot. |
| Fleet API error | Unchanged: the plain agent name, no dropdown. |

## Keyboard

- **Open**: Enter, Space or Down Arrow on the trigger (existing `Menu` behaviour). Focus goes
  to the filter box if it's shown, otherwise the first row.
- **Header row**: the filter and the **New agent** / **Fleet settings** icon buttons are a
  toolbar — **Left / Right Arrow** move across them (Right only hops out of the filter box once
  the caret is at the end). **Down Arrow** from a button drops into the list; **Up Arrow**
  returns to the filter. The buttons are plain `<button>`s (an `aria-label`, a `title` and the
  disabled-reason tooltip a `MenuItem` can't carry), and the DS Menu blocks Tab, so this toolbar
  wiring is what keeps them keyboard-reachable. Below the filter threshold (no filter box),
  **Up Arrow from the first row** reaches the first button instead.
- **Down / Up Arrow**: from the filter box, Down moves into the list. In the list, the arrows
  move through rows and group headers in order. Up from the first row goes back to the filter.
- **Right / Left Arrow** on a group header: open / collapse it. On an agent row, Right opens
  its "Move to group…" submenu (the standard `MenuSub` behaviour).
- **Enter** on a row switches agent. **Escape** closes and returns focus to the trigger.
- Typing a letter with focus in the list moves focus to the filter box and types it there
  (instead of Radix's first-letter typeahead).
- The star is a `MenuItem` `action`: Tab-reachable, not in the arrow cycle, as the DS
  documents. The submenu below gives a keyboard path to Pin as well.

## Row actions: "Move to group…" and Pin

Each agent row gets a `MenuSub` with the row's own options. It opens on hover or Right
Arrow, and on right-click of the row (via the `Menu` ref's `open({x,y})`).

- **Pin to top** / **Unpin**: toggles the star.
- **Move to group…**: a nested `MenuSub` listing every operator group in order, with a
  Check on the current one, then **No group**, a separator, and **New group…**.
  Picking a group moves the agent straight away and keeps the menu open, so the operator
  sees the row land. **New group…** opens a small inline name field (same `Input` as the
  filter). Enter creates the group and moves the agent into it.
- Remote agents show "Move to group…" disabled, with the tooltip
  "Remote agents are grouped by their machine." (Pin still works.)
- The submenu is hidden in the blocked member window (`fleetSettingsDisabledReason`),
  because there's no hub roster to write to there.

## Fleet settings: group editor

A new **Groups** section in Fleet settings. Live save: each change applies at once with a
small inline "Saved"; there is no Save button. Help line: "Groups organize the agent
switcher. They don't change what agents can do."

- **Group cards**, in switcher order. Each card header: drag handle plus Move up / Move down
  buttons (a keyboard-reachable reorder), the name (click to rename inline), the agent count
  Badge, and a Delete button. The card body lists that group's agents.
- **"No group" card** after the operator groups, holding ungrouped agents. Note: "These sit
  near the top of the switcher, without a heading." It can't be renamed, moved or deleted.
- **Remote card** last, read-only (see below).
- **Agent rows** inside every card: StatusDot, name ("this instance" Badge on the host), a
  pin star (`aria-pressed`, label "Pin {agent}" / "Unpin {agent}"), and a **Group** picker.
  Drag a row onto another card to move it; the picker is the keyboard and touch path.
- An empty group card reads: "No agents yet. Drag one here, or pick this group from an
  agent's menu."
- **Add group** button under the list: adds a row with its name field focused.
- **Delete** asks: "Delete the group "{name}"? Its {n} agents become ungrouped. No agents
  are removed." Buttons: **Delete group** (destructive) and **Cancel**.
- **Rename** rules: 1–40 characters, trimmed, unique ignoring case. "Remote" is reserved.
  Errors: "Give the group a name.", "There's already a group called "{name}".",
  ""Remote" is reserved for agents on paired machines."
- **Group picker** on each agent row: the DS `DropdownSelect`, options No group, then each
  group in order. Remote agents show "On {machine}" as read-only text in its place.
- The **Remote card** is listed last, read-only, with the note "Agents from paired machines
  are grouped automatically." Its rows can't be dragged and have no picker, but keep the pin
  star.

## Mobile

Status: **approved** (Josh, 2026-11-03; Decisions log 9).

Applies below the console's mobile breakpoint (`useIsMobile`, `max-width: 767px`), where
`MobileShell` renders the same `FleetSwitcher` in its identity slot. Touch has no hover,
right-click or reliable drag, so every action gets a visible control. Data, sections, copy
and rules are unchanged; only the container and the controls change.

**Switcher: a bottom sheet.** The DS `Drawer` with `side="bottom"` (already in
`@protolabsai/ui/overlays`: full width, up to 85% of the screen tall, Escape/backdrop close,
focus trap). Not a library gap.

- Tapping the agent name opens the sheet. Title: "Switch agent". The sheet has a Close
  button in its header, beside New agent and Fleet settings icon buttons, and closes on a backdrop tap or after switching.
- The filter sits fixed at the top of the sheet; the list scrolls under it. On a phone the
  filter does **not** take focus on open (that would pop the keyboard over half the list);
  tap it to type. The 10-agent threshold is unchanged.
- Same once-only sections and order: current, pinned, ungrouped, groups, Remote. No footer.
- Rows are at least 44px tall (WCAG 2.5.5 target; well past 2.5.8's 24px minimum). Group
  headers are the same height and toggle on tap; collapsed state is shared with desktop
  (same per-browser storage).
- **Each agent row has a visible "⋯" button** at its right edge (44×44 target), label
  "More for {agent}". It replaces the hover star and the right-click: pinned agents keep
  a filled star as a status mark, not a button.
- Long-press on a row is a shortcut to the same actions, never the only way.

**Sheet requirements (from Josh's phone-mode bug report, 2026-11-03):**

- **Opening a sheet never scrolls the page behind it.** Moving focus into a sheet uses
  `focus({ preventScroll: true })`, and keeping the focused row visible scrolls only the
  sheet's own list (set its `scrollTop`). Never `scrollIntoView`: it scrolls every scrollable
  ancestor, including the page under the sheet. While a sheet is open the background is
  scroll-locked (no scroll on `body` or the app shell).
- **The switcher sheet opens at a fixed tall height, with the list scrolling inside it.**
  Use `<Drawer side="bottom" size="85dvh">`: the DS Drawer's default bottom sheet is
  content-height (auto, capped at 85dvh), so without `size` the sheet shrinks and jumps as
  the filter narrows the list. The filter stays fixed at the top; the list is the only
  scroll container. The row-actions sheet keeps the default content height.

**Row actions: a second sheet.** "⋯" opens a bottom sheet titled with the agent's name:

- **Pin to top** / **Unpin**
- **Move to group…** opens the group list in the same sheet (a "Back" button in the header
  returns): each group with a Check on the current one, **No group**, and **New group…**
  (an inline name field with a **Create** button; Enter also works). Picking a group moves
  the agent, closes the sheet and returns to the switcher with the row highlighted.
- Remote agents: "Move to group…" is disabled with the line "Remote agents are grouped by
  their machine." shown as text (no tooltips on touch).
- Stacking: the action sheet replaces the switcher sheet, rather than sitting on top of it,
  so there is only ever one sheet and one focus trap. Closing it goes back to the switcher.

**Group editor: one column.** Same cards, same order, stacked full width.

- Assigning uses each row's **Group** picker. Drag is off on touch (no drag handles shown);
  it stays on desktop.
- Reordering uses **Move up** / **Move down** on each card header (44px targets).
- Each agent row wraps to two lines when needed: name and star on top, the Group picker
  full width beneath.
- Rename, delete and the delete confirm are unchanged; the confirm `Dialog` is already
  narrow-safe (`max-width: 100%`, `dvh` height).

**Copy added for mobile**

| Where | Text |
|---|---|
| Switcher sheet title | Switch agent |
| Sheet close button | Close |
| Row "⋯" button label | More for {agent} |
| Move list back button | Back |
| New group confirm button | Create |
| Remote move note | Remote agents are grouped by their machine. |

## Data

**Stored with the fleet roster (hub, shared by every browser):**

```ts
type FleetGroup = { id: string; name: string }; // id is stable; rename never changes it
type FleetStatus = {
  agents: FleetAgent[];
  groups?: FleetGroup[];   // array order = switcher order
};
type FleetAgent = {
  // …existing fields
  group?: string | null;   // FleetGroup.id; ignored when remote === true
  pinned?: boolean;
  pinned_at?: string;      // ISO time, gives the Pinned order
  machine?: string;        // remote only: paired machine's display name (falls back to url host)
};
```

- Keyed by the immutable `FleetAgent.id`, never the editable name (same rule as slugs).
- Deleting a group clears `group` on its agents. A `group` pointing at an unknown id is
  treated as ungrouped.
- All fields are optional, so an older hub (no `groups`) renders today's flat list as
  Ungrouped plus Remote, with nothing broken.
- New writes: create, rename, reorder and delete group; set an agent's group; pin or unpin.
  All hub-only, gated by `fleetSettingsDisabledReason` like the rest of Fleet settings.

**Stored per browser (`localStorage`):**

- `protoagent.fleet.collapsed`: the group ids (and `"remote"`) that are collapsed.

## Copy

| Where | Text |
|---|---|
| Trigger aria-label | Switch agent (unchanged) |
| Filter placeholder | Find an agent |
| Filter aria-label | Find an agent by name or group |
| Section titles | Pinned · {group name} · Remote |
| Remote row badge | {machine name} |
| Host badge | this instance (unchanged) |
| Group header aria-label | {name}, {n} agents, collapsed / expanded |
| Star action label | Pin {agent} to top / Unpin {agent} |
| Row submenu | Pin to top · Unpin · Move to group… |
| Move submenu | {groups} · No group · New group… |
| New group field placeholder | Group name |
| Remote move disabled tooltip | Remote agents are grouped by their machine. |
| No filter matches | No agents match "{text}" |
| Header icon buttons (tooltip and aria-label) | New agent · Fleet settings |
| Settings section | Groups |
| Settings help | Groups organize the agent switcher. They don't change what agents can do. |
| Settings buttons | Add group · Move up · Move down · Delete |
| Empty group (settings) | No agents yet |
| No groups (settings) | No groups yet. Add one to sort the switcher into sections. |
| Agent list column | Group |
| Remote note | Agents from paired machines are grouped automatically. |

## Gaps in the design system

1. **Collapsible menu section header.** Proposal: a `MenuGroup` in `@protolabsai/ui/menu`
   with `label`, `count`, `collapsed`, `onCollapsedChange`; renders a focusable header
   row (`aria-expanded`) that toggles without closing the menu. To file in protoContent
   as a `design-system` issue before the build.
2. **Text field inside a `Menu`.** Typeahead and arrow-key handover need a supported
   `MenuFilter` slot rather than a raw `Input` dropped in the content. Same issue or a
   sibling one.

**Decided (Josh, 2026-11-03): library first.** `MenuGroup` and `MenuFilter` go into
`@protolabsai/ui/menu` in protoContent. The console change waits until a library release
containing both is published (a cross-repo chain: DS first, then protoAgent adopts). No local
stand-ins in the console.

## Decisions log (Josh, 2026-11-03)

1. Library first for `MenuGroup` / `MenuFilter` (above).
2. One "Remote" section, with a machine Badge on each agent (not one section per machine).
3. Duplicates are fine: a pinned or recent agent also stays in its home section.
   _(Superseded by 11 — each agent now appears once and Recent is gone.)_
4. Remote agents can't move into operator groups ("Move to group…" disabled with tooltip),
   but can be pinned.
5. Pins live with the fleet roster (same in every browser); recents and collapsed groups
   are per browser. _(Recents retired by 11; collapsed groups stay per browser.)_
6. **Switcher preview approved** (Josh, 2026-11-03): reviewed
   `design/fleet-switcher-preview.html`, "looks good to me". Part 3 (the Fleet settings
   group editor preview) follows: `design/fleet-groups-settings-preview.html`.
7. **Group editor preview approved** (Josh, 2026-11-03): "looks fine". The group-cards
   layout it used is the design: each group is a card with its agents inside, plus a
   "No group" card and a read-only Remote card. It replaces the earlier "Groups list above
   the agent list" layout.
8. **Pins in settings too:** each agent row in the group editor has the same star as the
   switcher. Pins are one roster field, so either place changes both.
9. **Mobile approved** (Josh, 2026-11-03, after the fixed phone preview: "good for me, lets
   roll as suggested"). See the Mobile section. His three phone decisions:
   - the filter does not take focus when the sheet opens on a phone;
   - one sheet at a time (the actions sheet replaces the switcher sheet);
   - "⋯" replaces the tap-to-pin star on phones, and pinned agents keep a filled star as a
     marker.
10. **Spec APPROVED, ready to build** (Josh, 2026-11-03). Build order: `MenuGroup` and
    `MenuFilter` in protoContent first, then the protoAgent data, switcher and Fleet
    settings cards, all citing this file.
11. **Each agent once (Josh, 2026-11-04, supersedes 3)**: after using the shipped switcher
    Josh found a starred current agent listed three times (current row, Pinned, its section)
    — "awful ux … if an agent is active it only needs to appear in the main one" — and chose
    to **drop Recent**. So every agent now appears exactly once: the current agent only in the
    main row, other pinned agents only in Pinned, everyone else only in their home section
    (ungrouped / operator group / Remote). The **Recent** section is removed and the
    `protoagent.fleet.recent` storage key is retired — old values are ignored and switching
    writes no recents. Same screenshots asked to move **New agent** and **Fleet settings** out
    of the footer into the header row beside the filter (icon buttons, kept in view on scroll),
    and to keep the filter's search icon visible; the footer items and the separator before
    them are gone. The header icon buttons stay keyboard-reachable inside the DS Menu (which
    blocks Tab): Left/Right move across the filter and the two buttons, Down drops into the
    list, Up returns to the filter.
