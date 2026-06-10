# ADR 0048 — Settings IA: two scope-based homes (Host/App + Workspace)

- **Status:** Proposed (2026-06-10; structure locked in the operator walkthrough — see §3)
- **Date:** 2026-06-10
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** settings, ux, information-architecture, fleet, host, console
- **Related:** consumes [ADR 0047](./0047-layered-settings-cascade.md) (the
  per-field `Field.scope` host/agent cascade — this ADR is its UI surface);
  reorganizes surfaces from [ADR 0020](./0020-run-from-chat-manage-from-surfaces.md)
  (settings live in their home view), [ADR 0009](./0009-studio-control-stack.md)
  (the agent's makeup), and [ADR 0042](./0042-fleet-supervisor-unified-console.md)
  (fleet, slug routing, host = the first agent).

> ADR 0047 gave each field a `scope` (`host` | `agent`) and a real App→Host→Agent
> cascade. But the **console IA never reorganized around scope** — it still groups
> settings by *category* (Agent / System / Plugins tabs) and bolts the host view
> on as a cross-cut "Host defaults" tab, while an agent's actual *makeup*
> (Identity/SOUL, Tools, MCP, Subagents, Skills, Middleware) lives in a *separate*
> Agent view. The same agent's knobs are spread across three places with no clear
> "box-level vs workspace-level" line. This ADR makes **scope the primary axis**:
> exactly **two homes** — **Host / App** (box-shared, reachable from anywhere) and
> **Workspace** (the focused agent, everything that defines it).

## 1. Context & problem

Settings today are scattered across three structures (none scope-organized):

1. **Central Settings** (`SettingsSurface`) — tabs: Overview, Agents (fleet),
   Theme, Telemetry, Plugins, System, **Host defaults**. Routed by *category*
   (`_SECTION_CATEGORY` → Agent / Memory / Plugins / System, ADR 0020), with
   "Host defaults" added later as a `scope=="host"` cross-cut (ADR 0047).
2. **The Agent view** — tabs: Identity, Settings (`category="Agent"`), Tools, MCP,
   Subagents, Skills, Middleware (`App.tsx:537-543`). This is the agent's makeup
   (ADR 0009), but it's a *different* home from where the same agent's other
   knobs (the Agent-scoped central settings) live.
3. **Per-surface / plugin** — Theme (own surface), Delegates (a Plugins footer),
   plugin-contributed views.

Symptoms the operator hit:
- The "Host defaults" tab rendered **one full panel per category** (Agent + System)
  → two stacked Save bars / explainers (fixed as a stop-gap in #878, but the IA is
  the real cause).
- No answer to "is this setting for *this agent* or *the whole box*?" without
  knowing the per-field scope.
- "It's all clusterfucked into one panel, and spread about some plugins."

Meanwhile **the data model is already clean**: ADR 0047 tags 12 fields `host`
(the box-shared set) and the rest `agent`. The fix is IA, not schema.

### 1.1 The scope split today (ground truth, `graph/settings_schema.py`)

- **`scope="host"` (12 — box-shared, "set once, every agent inherits"):**
  `model.name` / `model.provider` / `model.api_base` (the gateway), `routing.aux_model`,
  `routing.fallback_models`, `prompt_cache.enabled` / `.ttl` / `.warm.enabled` /
  `.warm.interval_seconds`, `telemetry.enabled` / `.retention_days`, `identity.org`.
- **`scope="agent"` (the rest — per-workspace):** `identity.name` / `.operator`,
  `model.api_key` / `.temperature` / `.max_tokens` / `.max_iterations`, `compaction.*`,
  `goal.*`, `execute_code.*`, `knowledge.*`, `skills.top_k`, `checkpoint.*`,
  `middleware.*`, `operator.allowed_dirs`, `auth.token`, `agent_runtime`,
  `operator_mcp.tools`, `runtime.autostart_on_boot`.

## 2. The "host = the first agent" clarification

ADR 0047 §7 left host-defaults gated to "any focused agent, labeled box-shared"
as a TODO. Per ADR 0042 the **host IS a concrete agent — the first/primary one on
the box** (`slug=host`, self-registered, always present). So Host/App settings are
not an abstract box object: they are **the host agent's settings, which double as
the inherited defaults** for every other agent. This removes the "renders for any
agent, fudge-labeled" awkwardness — the Host/App home is shown *as the host*, and a
non-host workspace shows its own (overridable) values with the inherited-from-Host
badge ADR 0047 already provides.

## 3. Decision (locked in the walkthrough)

**Two scope-based homes. Scope is the primary axis.**

### 3.1 🖥 Host / App settings — box-shared, reachable from any workspace
"Set once for the box; every agent inherits (per-agent overrides win, ADR 0047)."

- The 12 `scope="host"` fields, grouped by section: **Model (gateway) · Routing ·
  Caching · Telemetry · Org**.
- App / box-level concerns that aren't per-agent: installed plugins + allowed
  sources (`plugins.sources.*`), the fleet roster (Agents), and box runtime
  (ports / bind / discovery / warm policy — currently scattered env/CLI reads,
  ADR 0047 §1 "no shared home"; surfacing these is a **follow-up**, not slice 1).
- One panel, one Save bar, one explainer (supersedes the per-category loop).
- Accessible from any workspace (it's the box's, not the focused agent's).

### 3.2 🧩 Workspace settings — the focused agent, everything that defines it
"Just for this workspace, while you're in it."

Folds **both** today's Agent-view makeup tabs **and** the agent-scoped central
settings into one home:
- **Identity** (name / SOUL / operator) · **Model overrides** (api_key,
  temperature, max_tokens — over the inherited host gateway) · **Behavior**
  (compaction, goals, knowledge, execute_code, checkpoint) · **Tools · MCP ·
  Subagents · Skills · Middleware** · **Theme** · this agent's **enabled plugins**.
- Each inherited-from-Host field keeps the ADR 0047 badge + reset-to-inherited.

### 3.3 What this removes / merges
- "Host defaults" tab → becomes the **Host / App** home (no per-category stacking).
- Agent-view tabs (Identity/Tools/MCP/Subagents/Skills/Middleware) → sections of
  **Workspace settings** (ADR 0009's "makeup" is preserved, just co-located with
  the rest of the agent's knobs — "manage from one place").
- Central Settings category tabs (Agent/System/Plugins) → dissolved into the two
  homes by scope, not category.

## 4. Slice plan

Scope-by-UI, smallest-blast-radius first; each slice build + e2e green, and (visual
slices) a DRAFT PR for the operator's local pass (CI can't judge UX — the
[UI local-test gate]).

1. **S1 — Host/App home** (mostly done via #878's one-panel `HostDefaultsPanel` +
   the `categories` prop): rename the tab/home to "Host / App settings", confirm it
   shows the full host set in one panel, drop the "· Agent/· System" framing.
2. **S2 — Workspace settings shell**: a single Workspace-settings home that renders
   the agent-scoped fields as sections + hosts the makeup panels (Identity/Tools/
   MCP/Subagents/Skills/Middleware) as sections rather than separate Agent-view tabs.
3. **S3 — Collapse the central category tabs** into the two homes; update the nav
   (Settings → {Host/App, Workspace}; Agents/Theme/Telemetry stay as their own
   surfaces or move under the appropriate home — decide in S3).
4. **S4 (follow-up)** — surface the box runtime concerns (ports/bind/discovery/warm)
   in Host/App (needs new host-level fields, ADR 0047 §1).

## 5. Consequences

- **+** One honest question answered everywhere: box-level vs this-workspace.
- **+** Removes the three-homes scatter; the agent's makeup + knobs are one place.
- **+** Leans entirely on ADR 0047's existing `scope` tags — no schema change for
  S1–S3.
- **−** A real nav reorg; muscle-memory churn for existing tabs. Mitigate with
  clear labels + the inherited-from-Host badges already in place.
- **Open:** exact placement of Agents (fleet) / Theme / Telemetry relative to the
  two homes (S3); whether per-agent "enabled plugins" management lives in Workspace
  while "install/sources" lives in Host/App (proposed: yes).
