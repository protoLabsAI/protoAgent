# Settings IA — target organisation

**Status:** FIRST PASS — proposed, awaiting review.
**This is the source of truth for the settings rework.** When a decision here conflicts with the
code, the code is wrong. When it conflicts with ADR 0047/0048, this doc supersedes them for IA
questions and the ADR gets amended in the same batch.

Baseline: [settings-ia-inventory.md](./settings-ia-inventory.md) — 101 fields, 8 categories,
3 unreachable, 17 chip-only, 2 core sections mis-filed by a default.

Every decision below is marked **PROPOSED** (Claude's first pass) → change to **DECIDED** or
override. Nothing is implemented until its decision is DECIDED.

---

## 1. The organising idea

**File a setting by what it controls, not by what it's named.**

Almost every defect in the inventory is a field filed by its key prefix or by word association
rather than by the thing it governs:

| field | filed under | actually controls |
|---|---|---|
| `egress.allowed_hosts` | Box ▸ **Network** | which hosts the **`fetch_url` tool** may reach |
| `model.max_iterations` | **Model** | the **agent loop** ("hard cap on the agent loop per turn") |
| `runtime.autostart_on_boot` | Behavior ▸ **Runtime** | a **macOS LaunchAgent** for the box |
| `fleet.autostart` | Box ▸ **Keep-warm** | **boot** policy (Keep-warm is LRU eviction) |
| `media.*`, `soul.*` | **Plugins** (by default fallback) | the **core** media store / **core** persona |
| `skills.top_k` | Knowledge ▸ **Recall** | the **Skills** library |

Filed correctly, most of the IA falls out and the three stranded fields get obvious homes.

---

## 2. Principles

| # | Principle | Fixes |
|---|---|---|
| P1 | **Every non-`ui_hidden` field has exactly one canonical home.** A field with no home is a bug. | D1 |
| P2 | **`ui_hidden` is the only legitimate way to keep a field out of the UI**, and must be justified at the declaration. | D1 |
| P3 | **A chip is a shortcut, never a home.** | chip sprawl |
| P4 | **File by what a field controls, not by its key prefix.** | D5, D7, D8, most re-files |
| P5 | **One operator intent = one control.** | D2 |
| P6 | **A setting is never rendered inside the thing it can destroy.** | D3 |
| P7 | **Misconfiguration fails loudly.** | D5, silent `?? sections[0]` |

> **DECISION P — adopt these principles?** · **PROPOSED: yes**

---

## 3. Target structure

> **DECISION A — Capabilities and Box get schema sections.** · **PROPOSED: A1**
>
> Each gains a schema-driven section alongside its bespoke managers — exactly the shape Agent
> already has (`Identity` bespoke + `Operator & access` schema). Follows the precedent set at
> `SettingsSurface.tsx:65-67` (*"a chip-in-a-dialog was unnecessary extra clicking"*). Gives the
> 20 orphaned fields a home (P1), makes chips shortcuts again (P3), and makes ADR 0048 §2.2 true
> rather than aspirational.

| Group | Section | Kind | Holds |
|---|---|---|---|
| Agent | Identity | bespoke | name, SOUL, `soul.self_edit_enabled` |
| Agent | Operator & access | schema | `identity.operator/org`, `auth.token`, `operator.project_dir`, `operator.allowed_dirs` |
| Agent | **Model & runtime** | schema | `model.*`, `routing.*`, `prompt_cache.*`, **+`agent_runtime`**, **+`operator_mcp.tools`**, **−`model.max_iterations`** |
| Agent | Behavior | schema | `compaction.*`, `goal.*`, `middleware.*`, `background.*`, `developer.channel`, **+`model.max_iterations`**, **−`agent_runtime`**, **−`runtime.autostart_on_boot`** |
| Agent | Knowledge | schema | `knowledge.*`, `checkpoint.*`, **−`skills.top_k`** |
| Agent | Secrets | schema | `secrets_manager.*` |
| Agent | Integrations | schema | **plugin-contributed fields only** |
| Capabilities | Tools · MCP · Skills · Subagents · Delegates | bespoke | the managers |
| Capabilities | **Tool access** *(new schema)* | schema | `filesystem.*`, `tools.disabled`, **+`egress.allowed_hosts`**, **+`skills.top_k`**, `skills.scope`, `commons.path`, `mcp.scope`, **+`media.*`** |
| Box | Overview · Fleet · Telemetry | bespoke | the managers |
| Box | **Box configuration** *(new schema)* | schema | `network.bind`, `fleet.*`, `telemetry.*`, **+`runtime.autostart_on_boot`** |
| This console | Theme · Chat · Keyboard · Developer | local | device prefs |

Sharing tiers (`knowledge.scope`, `skills.scope`, `mcp.scope`) stay **with their library**, not
collected into one "Sharing" section — an operator configuring knowledge looks under Knowledge.
This is already right today; noting it so the rework doesn't "fix" it.

---

## 4. Decisions

### DECISION B — `agent_runtime` → Model · **PROPOSED: B1 (yes)**
*Josh: "I would think agent runtime would just be in the model section"*

Move `agent_runtime` + `operator_mcp.tools` to **Model**, retitled **"Model & runtime"**.
The console already merged these axes for the operator: #1993 put ACP agents **in the model
picker**; #1995 makes runtime a per-chat choice from that same dropdown. If you pick it where you
pick a model, it configures where a model does.

⚠️ **ADR 0033 D1 holds runtime and model are separate axes** — a deliberate position. B1
contradicts it at the IA level. Proposed resolution: amend ADR 0033 D1 to say the axes remain
separate *in the model* (a runtime is not a model, and `acp:*` is not valid in an aux slot) but
are **presented together**, because the operator's question is "what drives this turn?".
→ **Needs your call: amend the ADR, or keep B2 and accept the picker/settings mismatch.**

`operator_mcp.tools` only means anything when `agent_runtime` is `acp:*`. It should also gain
`depends_on: agent_runtime` — it has none today, so it renders as a live field under a native
runtime where it governs nothing (same class as D4).

### DECISION C — kill the one-field "Runtime" section · **PROPOSED: C1**
*Josh: "the 'runtime' duplicate with the single autostart on reboot is annoying"*

- Delete the `Runtime` section (one field, a word away from `Agent runtime`, unrelated to it).
- `runtime.autostart_on_boot` → **Box ▸ Box configuration ▸ Boot**. It installs a LaunchAgent for
  the machine; that's box lifecycle, not agent behavior.
- New **Boot** section holds it + `fleet.autostart` (moved out of Keep-warm, which is LRU
  eviction — P4). The two "autostart" settings finally sit together, where their difference is
  visible instead of confusing.
- Relabel: `runtime.autostart_on_boot` → **"Start this instance at login"**;
  `fleet.autostart` → **"Members to start on boot"**.

⚠️ **Open bug:** `runtime.autostart_on_boot` is `scope=agent` but installs a *machine-level*
LaunchAgent. On a fleet box, a per-agent toggle for a box-wide LaunchAgent is incoherent — it
likely wants `scope=host`. **Needs a separate look; not an IA question.**

### DECISION D — fail loudly on unmapped sections · **PROPOSED: D1**

`_category_for()` currently defaults to `"Plugins"`, which is why two **core** sections file
themselves under Integrations. Proposed:

- A test asserting every declared `Field.section` is in `_SECTION_CATEGORY` (start here — cheap,
  catches it forever).
- `Media` → **Capabilities** (the core media store holds tool-generated output).
- `Persona` → **Identity** (it's SOUL).
- Then either drop the default (raise) or keep it only for genuinely plugin-contributed sections,
  which is what it was for.

### DECISION E — `checkpoint.*` stay in Knowledge · **PROPOSED: keep**

They're chat-history plumbing, but `checkpoint.harvest_enabled` summarises sessions *into* the
knowledge base — the domains genuinely touch. They're already their own `History` subsection,
which keeps the 25-field category scannable. Moving them buys churn, not clarity. Revisit only if
Knowledge grows again.

### DECISION F — reunite Skills; leave the operator dirs · **PROPOSED**

- `skills.top_k` → **Capabilities**, with `skills.scope`/`commons.path`. It configures the Skills
  library; it sits in Knowledge ▸ Recall only by analogy to `knowledge.top_k` (P4). **It also has
  no description** — write one either way.
- `operator.project_dir` / `operator.allowed_dirs` **stay in Operator & access**. Correcting the
  inventory: these fence the **tasks/notes APIs**, not the filesystem tools (`project_dir` doubles
  as the agent's default project). They're operator/console scope. Cross-reference them from the
  Tool access section rather than moving them.

### DECISION G — the two-layer tool control · **PROPOSED: G2, pending a backend answer**

Correcting the inventory: **both** layers act at graph build (`tools.disabled` = *"removed from
this agent's toolset at graph build"*). The real difference is only whether the tool stays
**catalogued** — a denylisted tool is listed `enabled:false` so it can be toggled back; a
config-unbound one isn't listed at all.

So the operator sees two switches for one intent (P5) that never agree.

- **G2 (proposed)** — keep both configs; **couple them in the UI**. The Filesystem group shows
  one state per tool; flipping `run_command` off writes whichever config is authoritative, and
  `filesystem.allow_run` reflects it rather than contradicting it.
- **G1** — make the row switch authoritative and retire `filesystem.allow_run`. Cleanest, but
  only if "never built" carries no security weight over "denylisted".
- **Blocking question for you:** is *"the tool is never built — the model can't see or call it"*
  a guarantee we intend to keep distinct from denylisting? The `allow_run` description sells it
  as "the full kill switch for shell access". If that distinction is real, **G1 is off** and G2
  is the ceiling.

### DECISION H — where filesystem settings render · **PROPOSED: H2**

Constrained by D3 (verified): `filesystem.enabled:false` removes the Filesystem group, so the
settings **cannot** live inside it (P6) — that's a one-way door out of the UI.

- **H2 (proposed)** — canonical home is **Capabilities ▸ Tool access** (from A1). The Tools panel
  keeps a **chip that deep-links there**, titled for the Filesystem group so it reads as that
  group's config. The chip becomes a true shortcut (P3), and the home survives
  `filesystem.enabled:false`.
- This is also the smallest change from today that satisfies Josh's *"keep the dialog, organise it
  with the filesystem group"* — the trigger sits with the group; the settings live somewhere that
  can't delete itself.
- **PR #2000 is invalidated by D3** and should be closed, not merged.

---

## 5. Decision log

| # | Question | Proposed | Status |
|---|---|---|---|
| P | Adopt the principles | yes | **PROPOSED** |
| A | Schema sections for Capabilities/Box | A1 | **PROPOSED** |
| B | `agent_runtime` → Model & runtime | B1 (+ amend ADR 0033 D1) | **PROPOSED — needs your call on the ADR** |
| C | Kill "Runtime"; `autostart_on_boot` → Box ▸ Boot | C1 | **PROPOSED** |
| D | Fail loudly on unmapped sections; Media→Capabilities, Persona→Identity | D1 | **PROPOSED** |
| E | `checkpoint.*` stay in Knowledge | keep | **PROPOSED** |
| F | `skills.top_k` → Capabilities; operator dirs stay | — | **PROPOSED** |
| G | Two-layer tool control | G2 | **BLOCKED — is "never built" a security guarantee?** |
| H | Filesystem settings home | H2 | **PROPOSED** |

---

## 6. Field moves (the whole diff)

Everything not listed stays put.

| field | from | to | why |
|---|---|---|---|
| `agent_runtime` | Behavior ▸ Agent runtime | Model ▸ Model & runtime | B1 — picked where models are picked |
| `operator_mcp.tools` | Behavior ▸ Agent runtime | Model ▸ Model & runtime | follows `agent_runtime`; **+ add `depends_on`** |
| `model.max_iterations` | Model ▸ Model | Behavior | "hard cap on the **agent loop**" — P4 |
| `runtime.autostart_on_boot` | Behavior ▸ Runtime *(section deleted)* | Box ▸ Boot | a LaunchAgent is box lifecycle — C1 |
| `fleet.autostart` | Box ▸ Keep-warm | Box ▸ Boot | Keep-warm is LRU eviction — P4. **Fixes D1** |
| `telemetry.fleet_trace_export` | Box ▸ Telemetry *(stranded)* | Box ▸ Telemetry *(reachable)* | **Fixes D1** — home was right, no panel existed |
| `egress.allowed_hosts` | Box ▸ Network | Capabilities ▸ Tool access | fences the **`fetch_url` tool** — P4. **Fixes D1** |
| `skills.top_k` | Knowledge ▸ Recall | Capabilities ▸ Skills | configures the Skills library — P4. **+ write a description** |
| `media.public`, `media.retention_days` | Plugins *(by default!)* | Capabilities ▸ Media | core media store, not a plugin — D5 |
| `soul.self_edit_enabled` | Plugins *(by default!)* | Identity | it's SOUL — D5 |
| `filesystem.*` (4) | Tools chip *(only door)* | Capabilities ▸ Tool access | H2 — home that can't delete itself |
| `tools.disabled` | Tools row switches | Capabilities ▸ Tool access | canonical home; rows stay as the shortcut |
| `mcp.scope` | MCP chip *(only door)* | Capabilities ▸ Tool access | P3 — chip becomes a shortcut |
| `skills.scope`, `commons.path` | Skills chip *(only door)* | Capabilities ▸ Skills | P3 |
| `network.bind`, `fleet.port_base`, `fleet.discovery.*`, `fleet.warm.*` | Fleet chip *(only door)* | Box ▸ Box configuration | P3 |
| `telemetry.enabled`, `telemetry.retention_days` | Telemetry chip *(only door)* | Box ▸ Telemetry | P3 |

**Net:** 3 stranded fields get homes · 17 chip-only fields get canonical homes (chips stay as
shortcuts) · 2 mis-defaulted core sections re-filed · 1 section deleted · 1 new section (Boot) ·
2 new schema panels.

---

## 7. Work unblocked by any decision

Defects under every target:

- [ ] `QuickSettingDialog` honours `depends_on` via `fieldVisible` (D4) — live bug.
- [ ] `operator_mcp.tools` gains `depends_on: agent_runtime` — same class.
- [ ] **23 of 101 fields have empty descriptions** — the row renders as a bare label with no
      explanation of what it does. Notably all 5 `middleware.*`, all 3 `identity.*`,
      `knowledge.top_k` / `skills.top_k`, `model.temperature` / `max_tokens` / `provider` /
      `api_base`, `goal.max_iterations`, `compaction.keep_messages`, `prompt_cache.ttl`,
      4 `secrets_manager.*`. No IA change fixes an unexplained switch.
- [ ] `SettingsSurface`'s `?? sections[0]` silently renders an unrelated panel — fail loudly (P7).
- [ ] Test: every declared section is mapped in `_SECTION_CATEGORY` (D5).
- [ ] Test: every non-`ui_hidden` field is reachable from some surface (D1 can't recur).
- [ ] `runtime.autostart_on_boot` scope=agent vs a machine-level LaunchAgent — likely `host`.

---

## 8. Sequencing

1. **Guardrails first** — the two tests + the `depends_on` fix. They're independent, they're
   bugs, and the section test will fail on Media/Persona immediately, proving D5.
2. **Panels** (A1) — add the two schema sections. Nothing moves yet; the 3 stranded fields
   become reachable, and the 17 chip-only fields gain a canonical home. Chips keep working.
3. **Re-files** (§6) — pure schema moves, one PR, no console change.
4. **Deletions/renames** — kill `Runtime`, add `Boot`, relabel the autostarts.
5. **G** — only once the never-built-vs-denylisted question is answered.
6. **Amend ADRs** — 0048 §2.2, 0033 D1 (if B1), and note on 0047 §7.7.

---

## 9. Superseded / to amend

- **ADR 0048 §2.2** — "a chip is a shortcut to the canonical field": false today for 17 of 19.
  A1 makes it true.
- **ADR 0033 D1** — runtime/model as separate axes. B1 needs it amended (see Decision B).
- **ADR 0047 §7.7** — box-shared defaults are host-only. Not in question, but "host-only" is
  about the **config cascade**, not about every Box-adjacent surface — the Fleet *roster* got
  lumped in by proximity (#1999). Worth a clarifying note.
