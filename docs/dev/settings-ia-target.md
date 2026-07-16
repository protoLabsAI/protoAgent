# Settings IA — target organisation

**Status:** WORKING DRAFT — decisions in progress.
**This is the source of truth for the settings rework.** When a decision here conflicts with the
code, the code is wrong. When it conflicts with ADR 0047/0048, this doc supersedes them for IA
questions and the ADR gets amended in the same batch.

Baseline being reworked: [settings-ia-inventory.md](./settings-ia-inventory.md) — 101 fields,
8 categories, 3 unreachable, 17 chip-only, 2 core sections mis-filed by a default.

Nothing here is implemented until the decision it depends on is marked **DECIDED**.

---

## 1. Principles

Proposed. These are what the individual placement calls should follow from, so they're worth
agreeing before the field-by-field work.

| # | Principle | Consequence |
|---|---|---|
| P1 | **Every non-`ui_hidden` field has exactly one canonical home.** | Fixes D1. A field with no home is a bug, not a style choice. |
| P2 | **`ui_hidden` is the only legitimate way to keep a field out of the UI** — and it must be justified at the declaration. | "Unreachable" and "deliberately YAML-only" stop being indistinguishable. |
| P3 | **A chip is a shortcut, never a home.** | Restores ADR 0048 §2.2 to being true. Implies Capabilities/Box need panels. |
| P4 | **A domain lives in one category.** | Fixes D8 (skills split, filesystem/dirs split). |
| P5 | **One operator intent = one control.** | Fixes D2. If two configs express "the agent has no shell", the UI shows one switch. |
| P6 | **A setting is never rendered inside the thing it can destroy.** | Fixes D3. Learned the hard way. |
| P7 | **Misconfiguration fails loudly.** | Fixes D5 (silent "Plugins" default) and the `?? sections[0]` silent misroute. |

> **DECISION P — adopt these principles?**
> Status: **UNDECIDED**

---

## 2. Target category structure

> **DECISION A — do Capabilities and Box get schema panels?**
> Status: **UNDECIDED**
>
> The Identity precedent (`SettingsSurface.tsx:65-67` — *"a chip-in-a-dialog was unnecessary
> extra clicking"*) says yes. This is the decision the other 20 fields hang off.
>
> - **A1 (recommended)** — Capabilities and Box each gain a schema section alongside their
>   bespoke managers, the way Agent has both `Identity` (bespoke) and `Operator & access`
>   (schema). Chips stay as shortcuts. Fixes D1 + P3 in one move.
> - **A2** — keep chips as homes; document that ADR 0048 §2.2 is aspirational and add the 3
>   missing fields to existing chips. Cheapest; leaves 17 fields reachable only by knowing
>   which panel hides a gear.
> - **A3** — dissolve Capabilities/Box as *schema* categories: re-file each field into a
>   category that already has a panel. No new panels; bigger semantic churn.

### Proposed shape under A1

| Group | Section | Kind | Holds |
|---|---|---|---|
| Agent | Identity | bespoke | name + SOUL |
| Agent | Operator & access | schema | `identity.*`, `auth.token` |
| Agent | Model | schema | `model.*`, `routing.*`, `prompt_cache.*`, **+ `agent_runtime`?** (Decision B) |
| Agent | Behavior | schema | `compaction.*`, `goal.*`, `middleware.*`, `background.*`, `developer.channel` |
| Agent | Knowledge | schema | `knowledge.*`, `skills.top_k`?, `checkpoint.*`? (Decisions E, F) |
| Agent | Secrets | schema | `secrets_manager.*` |
| Agent | Integrations | schema | **plugins only** — `media.*`/`soul.*` move out (Decision D) |
| Capabilities | Tools / MCP / Skills / … | bespoke | the managers |
| Capabilities | **Configuration** *(new)* | schema | `filesystem.*`, `tools.disabled`, `mcp.scope`, `skills.scope`, `commons.path` |
| Box | Overview / Fleet / Telemetry | bespoke | the managers |
| Box | **Configuration** *(new)* | schema | `network.*`, `egress.*`, `fleet.*`, `telemetry.*` |
| This console | Theme / Chat / Keyboard / Developer | local | device prefs |

Section names for the two new panels are a placeholder — "Configuration" is weak.

---

## 3. Open placement decisions

### DECISION B — where does `agent_runtime` live?
Status: **UNDECIDED** · *raised by Josh: "I would think agent runtime would just be in the model section"*

- **B1 (recommended)** — move `agent_runtime` (and `operator_mcp.tools`) to **Model**, retitle
  the section "Model & runtime". Matches what the console already does: #1993 put ACP agents
  **in the model picker**, and #1995 makes the runtime a per-chat choice made from that same
  dropdown. If the operator picks it where they pick a model, it configures where a model does.
- **B2** — keep in Behavior. **Tension:** ADR 0033 D1 holds runtime and model are *separate
  axes* — a deliberate position, not an accident. B1 contradicts it at the IA level even though
  the picker already merges them.
  → If B1: amend ADR 0033 D1 in the same batch, or say explicitly that the axes stay separate in
  the *model* but merge in the *UI*.
- `operator_mcp.tools` ("Restrict tools for the ACP brain") may belong in Capabilities instead —
  it's a tool allowlist, not a model knob. **Sub-question, undecided.**

### DECISION C — where does `runtime.autostart_on_boot` go, and does "Runtime" die?
Status: **UNDECIDED** · *raised by Josh: "the 'runtime' duplicate with the single autostart on reboot is annoying"*

The one-field `Runtime` section sits a word away from `Agent runtime` and holds a macOS
LaunchAgent installer — nothing to do with the agent runtime.

- **C1 (recommended)** — delete the `Runtime` section; move `runtime.autostart_on_boot` to
  **Box** (it's machine/box lifecycle, like `fleet.autostart`). Rename both for clarity:
  `runtime.autostart_on_boot` → "Start this instance at login"; `fleet.autostart` → "Members to
  start on boot".
- **C2** — move it to "This console". Wrong: it's an agent-scoped server-side config, not a
  device pref.
- **C3** — keep in Behavior, just rename the section.
- **Note:** it's `scope=agent` but installs a *machine-level* LaunchAgent. On a fleet box, what
  does a per-agent LaunchAgent toggle even mean? **Possible latent bug — needs its own look.**
- Also: `fleet.autostart` is filed under **Keep-warm** (LRU eviction). Boot policy isn't
  keep-warm. Proposed: a `Boot` section holding both.

### DECISION D — the "Plugins" default
Status: **UNDECIDED**

- **D1 (recommended)** — `_category_for()` raises on an unmapped section (or a startup warning +
  a test asserting every declared section is mapped). Map `Media` → Capabilities (it's the core
  media store) and `Persona` → Identity (it's SOUL). Satisfies P7.
- **D2** — keep the default, just add the two map entries. Leaves the trap for the next section.

### DECISION E — do `checkpoint.*` stay in Knowledge?
Status: **UNDECIDED**

6 fields of conversation-history retention/pruning inside a 25-field Knowledge category. They're
chat-history plumbing. Options: keep (they feed knowledge via `harvest_enabled`) · move to
Behavior · own "History" category.

### DECISION F — reunite the split domains (D8)?
Status: **UNDECIDED**

- `skills.top_k` (Knowledge ▸ Recall) vs `skills.scope` + `commons.path` (Capabilities).
  `skills.top_k` also has an **empty description** — fix regardless.
- `operator.project_dir` / `operator.allowed_dirs` (Identity) are the directory fence the
  filesystem tools operate in — arguably Capabilities, next to `filesystem.*`.

### DECISION G — collapse the two-layer tool control (D2)?
Status: **UNDECIDED** · *the hardest one*

Today `filesystem.allow_run` (never build) and `tools.disabled: [run_command]` (drop after
assembly) both mean "no shell", don't reconcile, and are shown as two switches that disagree.

- **G1** — make the row switch authoritative for per-tool on/off; demote `filesystem.allow_run`
  to a derived view of it (or retire it). Cleanest for P5; needs care — `allow_run` currently
  means "never *built*", which is a stronger security claim than "denylisted", and that
  difference may be deliberate.
- **G2** — keep both layers but couple the UI: the row switch reflects/writes whichever config
  is authoritative, and the group shows one state.
- **G3** — keep both, document the distinction, and make the UI explain it.
- **Blocking question:** is "never built" a security guarantee we intend to keep distinct from
  "denylisted"? If yes, G1 is off the table. *Needs a backend answer before deciding.*

### DECISION H — where do the filesystem settings render, given D3?
Status: **UNDECIDED**

`filesystem.enabled:false` removes the Filesystem group (verified) — so the settings **cannot**
live inside that group (P6). Josh's call: *keep the dialog, organise it with the filesystem
group.*

- **H1** — dialog trigger on the Tools panel (roughly today), but retitled/reorganised so it
  reads as belonging to the Filesystem group rather than the whole panel.
- **H2** — the settings live in the Capabilities ▸ schema section (Decision A1); the Tools panel
  keeps a chip that deep-links there. Chip becomes a true shortcut (P3), and the home survives
  `filesystem.enabled:false`.
- **H3** — trigger inside the group, but the group is rendered even when empty (backend must
  catalogue unbound-by-config tools like it catalogues denylisted ones). Fixes D3 at the source;
  backend change.

> H2 + A1 collapse into the same move. If A1 lands, H2 is nearly free.

---

## 4. Decision log

| # | Question | Status | Decided |
|---|---|---|---|
| P | Adopt the principles | UNDECIDED | — |
| A | Schema panels for Capabilities/Box | UNDECIDED | — |
| B | `agent_runtime` → Model | UNDECIDED | — |
| C | `runtime.autostart_on_boot` home; kill "Runtime" | UNDECIDED | — |
| D | "Plugins" default → fail loudly | UNDECIDED | — |
| E | `checkpoint.*` stay in Knowledge | UNDECIDED | — |
| F | Reunite split domains | UNDECIDED | — |
| G | Collapse the two-layer tool control | UNDECIDED | — |
| H | Where filesystem settings render | UNDECIDED | — |

---

## 5. Work that is unblocked regardless

These need no decision — they're defects under any target.

- [ ] Surface the 3 stranded fields (D1) — `egress.allowed_hosts`, `fleet.autostart`,
      `telemetry.fleet_trace_export`. *(Home depends on A, but "must be reachable" doesn't.)*
- [ ] `QuickSettingDialog` honours `depends_on` via `fieldVisible` (D4) — a live bug today.
- [ ] `skills.top_k` has no description.
- [ ] `SettingsSurface`'s `?? sections[0]` silently renders an unrelated panel for an unknown
      deep-link — should say so instead (P7).
- [ ] A test asserting every non-`ui_hidden` field is reachable from some surface, so D1 can't
      recur.

---

## 6. Superseded / to amend on completion

- **ADR 0048 §2.2** — "a chip is a shortcut to the canonical field". Currently false for 17 of
  19 chip-covered fields. Either make it true (A1) or amend the ADR (A2).
- **ADR 0033 D1** — runtime and model as separate axes. Revisit if B1 lands.
- **ADR 0047 §7.7** — box-shared config defaults are host-only. Not in question, but the Fleet
  *roster* was lumped in with box defaults by proximity (see #1999); worth a note that
  "host-only" is about the config cascade, not about every Box-adjacent surface.
