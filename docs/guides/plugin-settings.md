# Design plugin settings

Every protoAgent plugin's settings follow this standard: Settings ▸ Plugins ▸ Configure, the
manifest's `settings:` and `settings_tabs:`, and any settings page a plugin serves itself. It
came out of projectBoard's Configure dialog, which grew to 29 settings grouped by code area.
The labels were engineer jargon, one description ran eight lines, three save models shared one
screen, and per-field "restart" badges came with no way to restart
(projectBoard-plugin#617 is the overhaul).

Check a settings change against every rule below before opening the PR. Settings are UI, so
the operator checks them in the console before they merge (a DRAFT PR, or a board card with a
merge-hold).

## 1. Organize by the operator's question, not by code area

Each tab answers one question someone running the agent actually asks. A good default set:

| Tab | The question | Typical contents |
|---|---|---|
| **Overview** | Is it working? | the main on/off switch, live counts, problems each with a fix button, the pending-restart banner |
| **What it acts on** (Projects, Accounts, Channels…) | Where does it work? | a compact list with a health dot per row, and a side-panel editor |
| **How it works** (Coders, Models…) | Who does the work, and how much at once? | the few choices most people make, then **Advanced** collapsed |
| **What happens next** (Review & merge, Publishing…) | What happens to the output? | a pipeline of steps in order, with an on/off and attempts per step |
| **Maintenance** | (rarely touched) | self-update and download toggles, sync jobs, secrets |

- A tab with one or two settings folds into another tab. A plugin with fewer than about 6
  settings needs no tabs at all.
- Put the setting 90% of operators change at the top of its tab. Put tuning knobs under a
  collapsed **Advanced** section.
- Never group by implementation ("Automation", "Loop", "Internals").

## 2. One save model per tab

- **Live is the default.** A control applies when it changes, with a small inline "Saved" (or
  the error, inline, saying nothing changed). There's no Save or Discard on a live tab.
- Only use save-then-apply for a multi-field form that is invalid half-filled (adding a
  project, say). Then the form has its own Save, and the rest of the tab stays live.
- **Restart-only settings are collected, never just badged.** After saving one, the page shows a
  single banner, "N changes take effect after a restart", with **Restart now** where the
  host offers it (protoAgent#4490). Without that, it explains how to restart. Prefer making a
  setting live-reloadable (`reload()` / `LIVE_KNOBS`) over marking it `restart: true`.
- Never show a live control and a Save button in the same tab.

## 3. Words

- **Label:** what it does, in the operator's words. No internal names: not `br`, `dep_gate`,
  `coder.solve`, `best-of-k`, `max_mode_n`, or "delegate" where "coder" is meant. If a
  concept needs a name, use the one the operator sees elsewhere in the console.
- **Help: one sentence of consequence**, what changes when you flip it ("Up to 4 cards build
  at once"). Two lines at most at the dialog width. The reasoning, edge cases and history go
  in the plugin's docs page, linked as "Learn more". Add a test that pins a max description
  length so it can't drift back.
- Say the current state in words, not just with a toggle: "Only codex can build", "All coders
  can build", "Off: the board and tools still work, nothing is dispatched".
- No absolute paths or long commands as the main content of a row. Show `~/dev/x`, and put the
  full command under a collapsed detail in monospace that wraps.

## 4. Controls

- **Known values get a picker, never free text.** A delegate, workflow, model, project or
  merge method is a select of what's configured, with the default marked.
- Bounded numbers use a stepper with min and max, and show the consequence next to it.
- A choice between two behaviours is a labelled radio with the trade-off in a few words
  ("when the blocker **merges** (safer) / **opens a PR** (faster)"), never a select of
  enum values.
- On/off is a DS switch (`pl-switch`). A step that is off greys out its sub-settings.
- Guard rails in the control itself: you can't switch off the last coder, and a delete needs a
  confirm and says what blocks it.

## 5. Show health, not just configuration

- Every list row (project, account, channel) carries a status dot: passing, failing or
  unknown. The operator should see what's broken without opening anything.
- Problems are one plain line plus the action that fixes it, a button where possible.
  Never show a raw error string as the only signal.
- Repeated identical blocks get collapsed: if 14 projects route the same way, show the
  ladder once as "All 14 projects", and only list the ones that differ.

## 6. Build it the host's way

- A tab that needs more than scalar fields is a **plugin-owned page**:
  `settings_tabs: [{id, label, path: /plugins/<id>/config/<tab>}]`. Serve it as public
  page chrome holding **no data**. Every read and write goes through the operator-gated
  `/api/plugins/<id>/…` routes via the DS kit's `kit.apiFetch`. Copy projectBoard's
  `/config/projects` and `/config/routing` (`projects_view.py`, `routing_view.py`,
  `view/<tab>.{html,css,js}`).
- Load only after `kit.initPluginView(...)`. Don't add a `setTimeout` fallback: the first
  fetch would race the bearer and 401 on a fleet member.
- Use DS classes and tokens only (`pl-card`, `pl-switch`, `pl-progress`, `pl-dot`,
  `pl-callout`, `--pl-*`). No new colours, no external assets. Works in light and dark.
- A slow read (more than about half a second) shows a loading line ("Reading coder routing…"),
  never an empty tab.
- Settings live in Settings. Don't add a settings tab to the plugin's main view (projectBoard
  B4 first put Coder routing on the board page and had to move it).

## Before you open the PR

- [ ] Each tab answers one operator question, and the common setting is at the top.
- [ ] One save model per tab, with no live control next to a Save button.
- [ ] Restart-only changes are collected in the banner, or the setting was made live.
- [ ] Every label is jargon-free, and every help line is one sentence of at most two lines.
- [ ] Known values are pickers, numbers are steppers, two-way choices are labelled radios.
- [ ] Lists show health dots, problems have a fix action, identical blocks are collapsed.
- [ ] Plugin-owned pages hold no data, wait for `initPluginView`, and use DS classes only.
- [ ] Walked it in a real console (an isolated dev instance) and attached screenshots.
- [ ] DRAFT PR, so the operator checks it before it merges.
