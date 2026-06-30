# ADR 0048 — Settings & console-configuration IA (domain-first, scope-as-badge)

- **Status:** Accepted — ratified 2026-06-28 (supersedes the 2026-06-10 "two
  scope-based homes" proposal recorded below in §6, History)
- **Date:** 2026-06-10 (proposed) · 2026-06-28 (ratified)
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** settings, ux, information-architecture, fleet, host, console
- **Related:** consumes [ADR 0047](./0047-layered-settings-cascade.md) (the
  per-field `Field.scope` host/agent cascade — this ADR is its UI surface);
  reorganizes surfaces from [ADR 0020](./0020-console-ia-run-from-chat.md)
  (settings live in their home view), [ADR 0009](./0009-studio-control-stack.md)
  (the agent's makeup), [ADR 0042](./0042-fleet-supervisor-unified-console.md)
  (fleet, slug routing, host = the first agent), and the keybinding system
  [ADR 0063](./0063-keybinding-system.md).

> **One-line decision.** Settings is organized **by domain** (what a setting
> *does*), **scope is a per-field badge** (not a nav axis), device/console prefs
> are split into a **This console** group, and box ops stay in a host-only **Box**
> group. The dead "scope-first" machinery from the 2026-06 proposal is removed.

## 1. Why this was reopened (the honest current state, 2026-06-28)

The 2026-06-10 proposal (§6) was **never fully ratified**, and the console drifted
into **three competing taxonomies that don't agree**, plus orphaned remnants of two
abandoned designs. Concretely:

1. **Data model** (`graph/settings_schema.py`) routes by **4 categories** via
   `_CATEGORY_ORDER` (`Agent · Memory · Plugins · System`) and a `_SECTION_CATEGORY`
   map.
2. **Console sidenav** (`SettingsSurface.tsx`) shows **2 groups / 17 items**
   (`Agent[14]` + host-only `Box[3]`). Only **3** of those 17 items are
   category-driven (`Model & Routing`→`category="Agent"`, `Memory`, `System`); the
   other 14 are bespoke panels bolted on outside the category system.
3. **ADRs** describe a **third** shape (0047's "Host defaults" cross-cut; 0048's
   "two homes"), neither of which shipped.

Symptoms this produced:

- **A label that lies.** "Model & Routing" renders `category="Agent"`, which also
  contains Identity / Goal mode / Tools / Skills / MCP field groups — sections that
  *also* have their own dedicated sidenav items.
- **Same field, multiple doors / backends.** `identity.name` is editable in the
  schema panel (`/api/settings` cascade) **and** in the bespoke Identity panel
  (`/api/config`). `telemetry.enabled` has four doors (System sidenav · Box
  Telemetry · a QuickSetting chip · an AppDrawer shortcut). `skills.scope` and
  `skills.top_k` are split across two different panels.
- **Box runtime in the wrong place.** `Network / Discovery / Keep-warm` (host
  box-runtime) map to `category="System"` → they render under the **System** item,
  far from **Fleet**.
- **Dead code from abandoned designs.** `HostDefaultsPanel` + `HostConfigLocked`
  (`SettingsCategory.tsx`) are exported but never imported. `settingsScope` /
  `setSettingsScope` / `SettingsScope` (`uiStore.ts`) are stored, defaulted, and
  **never read** — the corpse of the "two homes" axis.

Operator's verdict: *"it's not settled and is getting out of hand … all becoming
long in the tooth."* Correct. This ADR picks **one** axis and deletes the rest.

### 1.1 The scope split (ground truth, `graph/settings_schema.py`) — unchanged

- **`scope="host"` (box-shared, "set once, every agent inherits"):**
  `model.name` / `model.provider` / `model.api_base` (gateway), `routing.aux_model`,
  `routing.fallback_models`, `prompt_cache.*`, `telemetry.enabled` /
  `.retention_days`, `identity.org`, and the host box-runtime fields
  (`network.* · discovery.* · keep-warm.*`).
- **`scope="agent"` (per-workspace):** everything else (`identity.name/.operator`,
  `model.api_key/.temperature/.max_tokens/.max_iterations`, `compaction.*`,
  `goal.*`, `knowledge.*`, `skills.*`, `checkpoint.*`, `middleware.*`,
  `operator.allowed_dirs`, `auth.token`, `agent_runtime`, `operator_mcp.tools`,
  `runtime.autostart_on_boot`).

This is sound; **scope is data, not navigation.** ADR 0047's inherited-vs-overridden
badge is the right surface for it. We keep the cascade and the badge; we drop the
idea that scope should be a top-level nav split.

## 2. Decision

### 2.1 Settings is organized by **domain**; scope is a **per-field badge**

The Settings surface stays **one surface** with a sidenav of **three groups**:

```
SETTINGS  (focused agent + this box + this console)
│
├ AGENT — what defines the focused agent (cascading config; ADR 0047 badge per field)
│  ├ Identity       name · persona (SOUL.md) · operator · org
│  ├ Model          gateway · provider · key · temperature · max-tokens · routing · caching
│  ├ Behavior       goal mode · compaction · agent runtime · middleware · autostart
│  ├ Capabilities   Tools · MCP · Skills · Subagents · Delegates
│  ├ Knowledge      recall (top-k · embeddings)
│  └ Integrations   Plugins · GitHub repos
│
├ BOX — box-wide ops (HOST CONSOLE ONLY)
│  ├ Overview       model / version / storage at a glance
│  ├ Fleet          members · discovery · network · keep-warm   ← box runtime lives here
│  └ Telemetry      cost / latency store
│
└ THIS CONSOLE — device-local preferences (NOT agent config; no cascade)
   ├ Theme          (/api/theme)
   ├ Chat display   token/cost footer, transcript prefs (uiStore)
   └ Keyboard       shortcuts (ADR 0063)
```

Six **Agent** domains replace the 14-item scramble. Scope appears only as the
existing inheritance badge (`inherited from host` · `box default` · `overridden ·
reset`). There is **no** scope nav axis.

### 2.2 The canonical "which door does a setting get?" decision tree

```
Is it device/console-local (this browser, no cascade)?
  └ yes → THIS CONSOLE group (Theme / Chat display / Keyboard)
  └ no  → it configures the focused agent (it cascades). Pick ONE domain:
          • who the agent IS .......................... Identity
          • the LLM connection, sampling, cache ....... Model
          • how it thinks / loops / decides ........... Behavior
          • what it can DO (capability wiring) ........ Capabilities
          • recall / RAG .............................. Knowledge
          • external services + plugins ............... Integrations

Then: is the field box-OPERATIONAL (telemetry store, fleet/discovery/keep-warm)?
  └ yes → it renders in the BOX group (host console only)
  └ no  → it renders in its domain, with a per-field scope badge if host-scoped
```

**Invariants (the anti-sprawl contract):**

- **One field, one editor.** A given key has exactly one canonical control with one
  save path. No field renders in two domains.
- **A QuickSetting chip is a *shortcut to* the canonical field, never a second
  editor.** It deep-links into the owning domain (or writes the *same* `/api/settings`
  key); it must not become a parallel save path. New chips require an owning domain.
- **Scope is a badge, never a door.** Host-scoped config fields stay in their domain
  with a `box default` / `inherited from host` badge. Only box-*operational* fields
  move to the Box group.
- **Device prefs never cascade** and live only in `This console`.

### 2.3 Data-model alignment (`graph/settings_schema.py`)

`_CATEGORY_ORDER` and `_SECTION_CATEGORY` are rebuilt so the **category IS the
domain** — the data model and the sidenav stop disagreeing:

```
_CATEGORY_ORDER = ["Identity", "Model", "Behavior", "Capabilities",
                   "Knowledge", "Integrations", "Box"]   # plugin sections default → Integrations

_SECTION_CATEGORY = {
  "Identity": "Identity",
  "Model": "Model", "Routing": "Model", "Caching": "Model",
  "Goal mode": "Behavior", "Compaction": "Behavior",
  "Agent runtime": "Behavior", "Middleware": "Behavior", "Runtime": "Behavior",
  "Tools": "Capabilities", "MCP": "Capabilities", "Skills": "Capabilities",
  "Knowledge": "Knowledge",
  "GitHub": "Integrations",
  "Telemetry": "Box", "Network": "Box", "Discovery": "Box", "Keep-warm": "Box",
  # unmapped (Discord/Google/etc. plugin sections) → "Integrations"
}
```

Note `skills.top_k` moves from the `Knowledge` section to `Skills` (Capabilities) so
the two skill knobs sit together.

### 2.4 Top-level console organization (the "long in the tooth" pass)

Separate planes by **what you're doing**, and collapse redundant doors:

- **Operate** — the rail, unchanged: `Chat · Work · Knowledge` + plugin views.
- **Configure** — **one** Settings door (the utility-bar pill; the AppDrawer entry
  points at the same dialog; ⌘K deep-links to a group/section; `⌘,` opens it). The
  surface is §2.1's three groups.
- Remove the **redundant AppDrawer "Telemetry" shortcut** — Telemetry is the Box
  group; Settings is the single door.
- Activity stays a utility widget; Notes stays a utility widget (they're not config).

"This console" satisfies the *Preferences* split (device prefs visibly separated)
**without** adding a new top-level pill — fewer doors is the point.

## 3. What gets removed / fixed (cleanup ledger)

| # | Item | Action |
|---|------|--------|
| C1 | `HostDefaultsPanel`, `HostConfigLocked` (`SettingsCategory.tsx`) | ✅ **Done** — deleted (orphaned ADR-0047 UI, never imported). |
| C2 | `settingsScope` / `setSettingsScope` / `SettingsScope` (`uiStore.ts`) | ✅ **Done** — deleted; store bumped to v14 (drops it); `uiStore.test.ts` covers the v14 migration. |
| C3 | `SettingsSurface.tsx` sidenav | ✅ **Done** — rebuilt into domain groups (see *As-built* below). |
| C4 | "Model & Routing" panel rendering `category="Agent"` | ✅ **Done** — split into per-domain panels; the Model item now renders `category="Model"`. |
| C5 | `identity.name` dual path (schema vs `/api/config`) | ✅ **Already fixed** (2026-06-28) — `identity.name` is `ui_hidden` in the schema (#1076); the Identity panel owned it via `/api/config`. *(Superseded by **T2** (#1428): the name now saves via the canonical `/api/settings` cascade; see §6.)* |
| C6 | `skills.top_k` in the `Knowledge` section | ⚠️ **Deviation — left in Knowledge.** Moving it to a `Capabilities`-only home would orphan it (no rendered Capabilities panel covers it cleanly), and it's recall-adjacent. Kept under Knowledge. |
| C7 | `Network/Discovery/Keep-warm` under System | ✅ **Done** — re-homed to the **Box** domain (rendered in the host-only "Box config" item). |
| C8 | AppDrawer "Telemetry" shortcut | ✅ **Done** — removed; Settings is the single door (Telemetry = a Box section / ⌘K deep-link). |
| C9 | QuickSetting chips (`mcp.scope`, `skills.scope`, `knowledge.*`, `telemetry.*`) | ✅ **Kept as shortcuts** — all write the same `/api/settings` key as their canonical domain panel (no second save path), so they satisfy §2.2. The canonical full editors are the "Sharing & tiers" (Capabilities) and "Box config" (Box) panels. |
| C10 | `uiStore` default `settingsSection: "overview"` | ✅ **Done** — default is `identity`; the v14 migration remaps old ids (`overview/settings/memory/system/middleware`). |

### 3.1 As-built sidenav (2026-06-28)

The §2.1 sketch put everything under one "Agent" group; the implementation splits the
makeup into its own **Capabilities** group (so the rich Tools/MCP/Skills/Subagents/Delegates
managers keep first-class items) and adds two schema-home items for fields that had no
bespoke editor:

```
Agent         Identity · Model · Behavior · Knowledge · Integrations
Capabilities  Tools · MCP · Skills · Subagents · Delegates
Box (host)    Overview · Fleet · Telemetry
This console  Theme · Chat · Keyboard
```

- **Identity** is the bespoke panel ALONE (name + SOUL), so the SOUL editor fills the
  panel. The operator/org/access schema fields (operator · org · project dir · allowed
  dirs · A2A token) hang off an "Operator & access" **chip** in its header.
  *(An earlier build composed Identity + a schema panel; two `flex:1` `.stage-panel`s
  split the height 50/50 — the SOUL editor only filled half and it read as two confusing
  panels. The chip avoids both.)*
- **Integrations** is the renamed Plugins item (id stays `plugins` for the ⌘K/deep-link
  contract). GitHub is a plugin, so it lands here by default.
- **No standalone schema-only items.** The sharing/tier + box-runtime knobs are reached
  via contextual **chips** on the relevant manager (Skills chip = `skills.scope` +
  `commons.path`; MCP chip = `mcp.scope`; Fleet chip = box-runtime; Telemetry chip =
  telemetry store) — same `/api/settings` save path (§2.2), no empty panels.
- The read-only **Middleware** roster panel was removed; its editable toggles live in
  the **Behavior** domain.

## 4. Slice plan (smallest blast radius first; each build + e2e green)

Visual slices land as **DRAFT** PRs for the operator's local pass (CI can't judge
UX — the UI local-test gate).

1. **S1 — Dead-code removal (zero UX change):** C1 + C2. Pure deletion + store
   version bump. Safe to merge on green.
2. **S2 — Data-model domains:** C6 + C7 + §2.3 (`_CATEGORY_ORDER` /
   `_SECTION_CATEGORY`). Update `tests/test_config_roundtrip.py` golden map. No
   frontend change yet (categories are internal).
3. **S3 — Sidenav reshape:** C3 + C4 + C10 — render the three groups and the six
   Agent domain panels. DRAFT.
4. **S4 — De-dup the bespoke panels:** C5 (Identity single path) + C9 (QuickSetting
   audit). DRAFT.
5. **S5 — Top-level polish:** C8 + the `This console` group labeling + ⌘K
   deep-links per §2.4. DRAFT.

## 5. Consequences

- **+** One honest axis (domain) the data model and UI both speak; scope answered by
  the badge that already works.
- **+** 17 flat items → 6 Agent domains + Box + This-console; one door per field.
- **+** Deletes two designs' worth of dead code and the lying label.
- **−** Real nav reorg + muscle-memory churn; `_SECTION_CATEGORY` change touches the
  config-roundtrip golden map (PROTO.md gotcha). Mitigated by the slice plan + the
  inheritance badges already in place.
- **Open:** whether `Capabilities` should eventually graduate to its own top-level
  rail destination (it's the agent's "makeup") — deferred; it stays a Settings
  group for now.

## 6. True-up & DS extraction (2026-06-29 — finishing "one system" + moving primitives upstream)

The domain-first IA (§§2–4) shipped (#1393). A follow-up four-dimension audit — canonical-system
map · panel inventory · config dual-paths · DS contribute-back — asked the harder question: does
**every** settings/view/panel actually use the one system, and what UI logic should leave the app
for the design system? Finding: the schema-cascade `/api/settings` pipeline is canonical and
broadly adopted (QuickSetting chips, `PluginSettingsDialog`, and all host/box-runtime knobs share
it; the legacy `hostLayer` `SettingsCategory` path is dead code). The residual drift is narrow and
named — this is *finish-and-extract*, not a rebuild.

### 6.1 Canonical system (the contract every config surface must meet)

A setting is one backend `Field` (`graph/settings_schema.py`) → `build_schema()` JSON → the generic
`SettingInput` renderer → `POST /api/settings` (cascade-aware, ADR 0047). Add a `Field`, restart,
and it renders + validates + saves with zero frontend code. Reuse, not reimplementation:
`QuickSetting` (contextual chip/dialog) and `PluginSettingsDialog` (a Dialog wrapping
`SettingsCategory`) both render through the SAME path. Bespoke managers (Identity; the
Tools/MCP/Skills/Subagents/Delegates makeup; Box; This-console prefs) are legitimate where they're
rich CRUD or device-local — but they must (a) save through the canonical path for any field the
schema owns, (b) report via DS `useToast`, and (c) use DS form primitives, not hand-rolled inputs.

### 6.2 True-up ledger (T-series)

| # | Item | Decision / status |
|---|------|-------------------|
| T1 | `IdentityPanel` echoed a **cached** `identity.operator` on every save → could clobber a fresh Operator & access edit | ✅ **Fixed (#1428)** — operator is owned solely by Operator & access; IdentityPanel never writes it. |
| T2 | `identity.name` lived in two systems (ui_hidden `Field` + bespoke `/api/config`) — supersedes C5 | ✅ **Fixed (#1428)** — name saves via the canonical `/api/settings` cascade; `ui_hidden` stays so IdentityPanel remains its **single editor**. SOUL (not a schema field) saves via `/api/config` with a null config. |
| T3 | `FleetManagerPanel` hand-patched `plugins.enabled` via `/api/config` | ✅ **Fixed (#1428)** — uses the dedicated `/api/plugins/{id}/enabled` endpoint (enabled/disabled reconcile + builtin guard). |
| T4 | Playbooks/Knowledge route errors through an `onError` prop that's a **no-op when embedded** → silent failures | ⬜ **Phase 1** — replace with DS `useToast`. |
| T5 | Toast convention half-adopted — inline status in McpPanel / PluginsSurface / InstallPluginDialog / McpCatalogDialog | ⬜ **Phase 2** — sweep to `useToast`. |
| T6 | Playbooks + Knowledge still on the retired manual-fetch pattern (not TanStack Query) | ⬜ **Phase 2.** |
| T7 | DS-adoption misses: raw `<checkbox>`→`Switch`, `window.confirm`→`ConfirmDialog`, `HelpLink`→`TextLink`, raw `<kbd>`→`Kbd` | ⬜ **Phase 2.** |
| T8 | SetupWizard `/api/config/setup` + SOUL writes | ✅ **Intentional** — first-run bootstrap, gated; left as-is. |

### 6.3 DS extraction (contribute upstream to `@protolabsai/ui`)

The settings renderer's **business logic stays app-owned** (save cascade, ADR-0047 inheritance,
`depends_on` visibility — domain, not UI). What moves upstream are the **primitives** it leans on,
each a genuine DS gap with app-agnostic reuse:

| Priority | Add to DS | Removes from the app |
|---|---|---|
| **P0** | `Button loading` prop (spinner + disabled + `aria-busy`) | a per-call ternary in ~16 files, the app `.spin` keyframe, the `RefreshButton`/`TestConnectionButton` wrappers |
| **P1** | `ToastProvider position` prop | the top-right `.pl-toast-stack` CSS override (closes protoContent#347) |
| **P1** | headless context-menu registry + host that renders DS `Menu` | ~120 LOC (`contextMenu/{registry,store,ContextMenuRenderer,types}`); app keeps only domain `registrations.tsx` |
| **P2** | `FieldControl` (type→control resolver), `PropertyRow`, `SecretInput(isSet)`, `TabBar onTabContextMenu`, `KeyRecorder` | the `SettingInput` switch + property-row chrome + masked-input duplication |

Delivered as the full contribute-back loop (build in `protoContent` → PR → DS release → bump
`@protolabsai/ui` → adopt in-app), leading with P0 (smallest DS change, largest app reduction).

**Delivered (2026-06-29 — the full loop, autonomous):**

| Item | DS | App adopt |
|---|---|---|
| `ToastProvider position` | shipped (0.49.0) | #1438 |
| `Button loading` (spinner + disabled + `aria-busy`) | protoContent #363 → **0.50.0** | #1439 |
| `Input` `icon` + reuse segmented `Tabs` for the filter chips | protoContent #366 → **0.51.0** | #1441 |
| `SecretInput` (password + reveal toggle) | protoContent #369 → **0.52.0** | #1442 |

Reclassified during delivery: the "SegmentedControl/ChipGroup" need is met by the **existing**
`Tabs variant="segmented"` (no new component). `FieldControl`/`PropertyRow` proved **redundant with
the existing `FormField`**, and the `SettingInput` type→control switch is domain logic that stays
app-side — so `SecretInput` is the one reusable primitive extracted from that surface. The headless
**context-menu kit** is tracked separately in protoContent #341 (in flight); `TabBar
onTabContextMenu` / `KeyRecorder` remain future DS gaps.

### 6.4 Slice plan (true-up)

- **Phase 0** — this section (record + ledger).
- **Phase 1 (correctness, auto-merge on green):** T1–T3 (#1428, done) · T4. Identity (T1/T2) folded in.
- **Phase 2 (conventions, DRAFT under the UI local-test gate):** T5–T7 + delete the dead `hostLayer` path.
- **Phase 4:** the §6.3 DS extraction wave. — ✅ **DONE 2026-06-29** (DS 0.50.0–0.52.0; app
  #1438/#1439/#1441/#1442). Context-menu kit (protoContent #341) tracked separately.

## 7. History — superseded 2026-06-10 proposal ("two scope-based homes")

> Retained for context. The original decision made **scope the primary axis** —
> exactly two homes, **Host/App** (box-shared) and **Workspace** (focused agent) —
> with category tabs dissolved into the two. The "host = the first agent"
> clarification (Host/App settings *are* the host agent's settings, which double as
> inherited defaults; ADR 0042) **still holds** and underpins §2's scope badge. What
> was dropped: scope-as-navigation. In practice the console shipped a single surface
> with `Agent`/`Box` groups and a half-built `settingsScope` axis that no view read;
> rather than finish that axis, §2 makes **domain** the axis and **scope** a badge.
> The proposal's slice plan (S1 Host/App home → S4 box runtime) is replaced by §4.
