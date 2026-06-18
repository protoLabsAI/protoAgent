// Persisted UI/layout state (ADR 0035 D5 — slice 1).
//
// The single source of truth for *navigation/layout* state: which surface is active,
// which sub-tab, the right panel's width/collapse. Zustand + `persist` → localStorage, so a
// refresh restores exactly where the user was (these were React `useState` before, lost on
// reload). UI state ONLY — server data stays in react-query; the two never mix.
//
// Later layout slices (dual rails, swap, mobile quick-bar) extend this store; today it mirrors
// the existing single-left-surface + right-panel model 1:1 so slice 1 is a pure state migration
// with no visible change.

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

// Per-agent layout (ADR 0042). Each fleet agent keeps its OWN layout — rail order, widths,
// active surface, plugins out. In the single-agent product that fell out for free (each agent
// is its own origin → its own localStorage); the unified console collapses that, so we namespace
// the persisted key by the agent. With slug routing (ADR 0042) the agent IS the URL slug
// (/app/agent/<slug>/), so derive the layout key from the URL at module load — each window keys
// its own layout, deterministically, no switch event needed. host = the legacy un-suffixed key.
let _layoutAgent = (() => {
  try {
    const m = globalThis.location?.pathname?.match(/\/agent\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  } catch {
    return "";
  }
})();
const _layoutStorage = createJSONStorage(() => ({
  getItem: (name: string) => globalThis.localStorage.getItem(_layoutAgent ? `${name}:${_layoutAgent}` : name),
  setItem: (name: string, value: string) =>
    globalThis.localStorage.setItem(_layoutAgent ? `${name}:${_layoutAgent}` : name, value),
  removeItem: (name: string) => globalThis.localStorage.removeItem(_layoutAgent ? `${name}:${_layoutAgent}` : name),
}));

// Core surfaces are fixed literals; plugin views (ADR 0026) add dynamic surfaces keyed
// `plugin:<pluginId>:<viewId>`. The `(string & {})` keeps literal autocomplete while allowing
// those runtime keys.
// The "agent" surface folded into Settings ▸ Workspace (ADR 0048 S-C); Knowledge is
// now store-only (its Memory settings live in Settings ▸ Workspace ▸ Memory).
export type Surface =
  | "chat" | "activity" | "studio" | "knowledge" | "plugins" | "box" | "settings" | (string & {});
// `notes` is no longer a built-in right panel — it's the first-party `notes` plugin
// (keyed `plugin:notes:<view>`), so it falls under the open `(string & {})` arm.
export type RightPanel = "beads" | "goals" | (string & {}); // + plugin:<id>:<viewId>
// Two sections (ADR 0059 D4): "local" = Installed (+ advanced install-from-URL),
// "market" = Discover. (Keys kept for persisted-state compat; the old "download"
// tab is gone — a stale persisted value falls back to Installed.)
export type PluginsTab = "local" | "market";
// "schedule" joins thread/inbox here (#1075): cron is a trigger, so timed turns are a
// tab of the Activity surface rather than a standalone rail surface.
export type ActivityTab = "thread" | "inbox" | "schedule";
// The Box surface (PR4 / ADR 0048 §5) — box-level operations that aren't per-agent
// cascade settings, moved out of the Settings ▸ Global home into their own rail surface.
export type BoxTab = "fleet" | "telemetry" | "commons";
// Settings IA (ADR 0048): scope is the primary axis — two homes, each with its own
// section sub-nav. `settingsScope` picks the home; `settingsSection` the active
// section within it (a free string so each home owns its own section ids).
export type SettingsScope = "host" | "workspace";

type UIState = {
  surface: Surface;
  rightPanel: RightPanel;
  pluginsTab: PluginsTab;
  boxTab: BoxTab;
  settingsScope: SettingsScope;
  settingsSection: string;
  // One-shot: the FleetSwitcher's "+ New agent" deep-link routes to Host/App ▸ Fleet
  // and asks the fleet panel to open the new-agent picker on mount, then clears it.
  fleetStartNew: boolean;
  activityTab: ActivityTab;
  rightCollapsed: boolean;
  leftCollapsed: boolean;
  rightWidth: number;
  // Ordered surface lists per rail (ADR 0035 D2 + 0036) — a surface is on exactly one side, at a
  // position. Core surfaces seeded below; plugin views append by their manifest `placement`. Chat
  // is pinned left (mounts unconditionally for streaming continuity) — never moved across rails.
  // Three docks now (DS AppShell bottom dock): left/right rails + the bottom dock (a
  // horizontal icon rail in the util bar + a full-width panel). A surface is on exactly
  // one dock, at a position.
  railOrder: { left: string[]; right: string[]; bottom: string[] };
  moveSurface: (id: string, side: "left" | "right" | "bottom") => void; // splice out → append to the target dock
  reorderSurface: (id: string, dir: -1 | 1) => void; // swap with the neighbour within its rail
  setRailOrder: (next: { left: string[]; right: string[]; bottom: string[] }) => void; // DS AppShell DnD — whole new order
  // Sync plugin views into railOrder (ADR 0036) — append newly-available ones to their placement
  // side, prune `plugin:` ids no longer present. Core surfaces are left untouched.
  reconcilePluginViews: (views: { id: string; side: "left" | "right" | "bottom" }[]) => void;
  // Bottom dock — active surface + height + collapse (mirror the right panel, on the Y axis).
  bottomPanel: string;
  bottomHeight: number;
  bottomCollapsed: boolean;
  // Mobile shell (ADR 0035 S4): one active surface + a configurable bottom quick-bar.
  mobileActive: string;
  setMobileActive: (id: string) => void;
  quickBar: string[]; // surfaces pinned to the mobile bottom bar (cap 5)
  toggleQuickBar: (id: string) => void;
  setSurface: (s: Surface) => void;
  setRightPanel: (p: RightPanel) => void;
  setPluginsTab: (t: PluginsTab) => void;
  setBoxTab: (t: BoxTab) => void;
  setSettingsScope: (s: SettingsScope) => void;
  setSettingsSection: (s: string) => void;
  setFleetStartNew: (b: boolean) => void;
  setActivityTab: (t: ActivityTab) => void;
  setRightCollapsed: (b: boolean) => void;
  setLeftCollapsed: (b: boolean) => void;
  setRightWidth: (w: number) => void;
  setBottomPanel: (p: string) => void;
  setBottomHeight: (h: number) => void;
  setBottomCollapsed: (b: boolean) => void;
  // Notification dots (ADR 0039) — a plugin surface key (`plugin:<id>:<view>`) with unseen
  // bus activity shows a rail dot until opened. Persisted so the dot survives a refresh.
  pluginDots: Record<string, boolean>;
  setPluginDot: (key: string, on: boolean) => void;
};

/** persist v1→v2 migration: drop the obsolete `railOf` (side map); `railOrder`
 * falls back to the default via the store's merge. Exported for unit testing. */
export function migrateUiState(persisted: unknown): unknown {
  if (persisted && typeof persisted === "object") {
    // v2: drop the obsolete `railOf` (side map). v3 (ADR 0048): drop `settingsTab`
    // (→ `settingsScope` + `settingsSection`) and the `agentTab` / `knowledgeTab`
    // keys whose surfaces folded into Settings ▸ Workspace. All fall back to the
    // store defaults via the persist merge. (A stale "agent" left in `railOrder` is
    // harmless — railSurfaces() filters ids with no surface metadata.)
    const {
      railOf: _drop,
      settingsTab: _drop2,
      agentTab: _drop3,
      knowledgeTab: _drop4,
      ...rest
    } = persisted as Record<string, unknown>;
    // v4 (PR4): the "Box" rail surface (Fleet/Telemetry/Commons moved out of Settings ▸
    // Global). Inject it into a persisted railOrder that predates it — just before
    // "settings" — so an existing drag-and-drop layout gains the surface instead of
    // silently hiding it. (A fresh store seeds it via the default railOrder above.)
    const ro = rest.railOrder as { left?: string[]; right?: string[] } | undefined;
    if (ro && Array.isArray(ro.left) && !ro.left.includes("box") && !(ro.right ?? []).includes("box")) {
      const left = ro.left.slice();
      const at = left.indexOf("settings");
      left.splice(at >= 0 ? at : left.length, 0, "box");
      rest.railOrder = { ...ro, left };
    }
    // v5 (#1075): "schedule" folded into the Activity surface (a tab), so prune it from a
    // persisted railOrder — otherwise it lingers as a dead rail id with no surface metadata.
    const ro2 = rest.railOrder as { left?: string[]; right?: string[] } | undefined;
    if (ro2 && (Array.isArray(ro2.left) || Array.isArray(ro2.right))) {
      rest.railOrder = {
        left: (ro2.left ?? []).filter((x) => x !== "schedule"),
        right: (ro2.right ?? []).filter((x) => x !== "schedule"),
      };
    }
    // v6 (bottom dock): railOrder gains a `bottom` dock — add the empty array to a
    // persisted layout that predates it so the shape is complete.
    const ro3 = rest.railOrder as { left?: string[]; right?: string[]; bottom?: string[] } | undefined;
    if (ro3 && !Array.isArray(ro3.bottom)) {
      rest.railOrder = { ...ro3, bottom: [] };
    }
    return rest;
  }
  return persisted;
}

export const useUI = create<UIState>()(
  persist(
    (set) => ({
      surface: "chat",
      rightPanel: "beads",
      pluginsTab: "local",
      boxTab: "fleet",
      settingsScope: "host" as SettingsScope,
      settingsSection: "overview",
      fleetStartNew: false,
      activityTab: "thread",
      rightCollapsed: false,
      leftCollapsed: false,
      rightWidth: 360,
      bottomPanel: "",
      bottomHeight: 240,
      bottomCollapsed: false,
      railOrder: {
        left: ["chat", "activity", "studio", "knowledge", "plugins", "box", "settings"],
        right: ["beads", "goals"],
        bottom: [],
      },
      moveSurface: (id, side) =>
        set((s) => {
          const arrs = {
            left: s.railOrder.left.filter((x) => x !== id),
            right: s.railOrder.right.filter((x) => x !== id),
            bottom: s.railOrder.bottom.filter((x) => x !== id),
          };
          arrs[side].push(id); // append to the target dock's end
          return { railOrder: arrs };
        }),
      reorderSurface: (id, dir) =>
        set((s) => {
          const swap = (arr: string[]) => {
            const i = arr.indexOf(id);
            const j = i + dir;
            if (i < 0 || j < 0 || j >= arr.length) return arr;
            const next = arr.slice();
            [next[i], next[j]] = [next[j], next[i]];
            return next;
          };
          return { railOrder: { left: swap(s.railOrder.left), right: swap(s.railOrder.right), bottom: swap(s.railOrder.bottom) } };
        }),
      setRailOrder: (railOrder) => set({ railOrder }),
      mobileActive: "chat",
      setMobileActive: (mobileActive) => set({ mobileActive }),
      quickBar: ["chat", "activity", "knowledge", "plugins"],
      toggleQuickBar: (id) =>
        set((s) => {
          if (s.quickBar.includes(id)) return { quickBar: s.quickBar.filter((x) => x !== id) };
          if (s.quickBar.length >= 5) return s; // cap the bottom bar
          return { quickBar: [...s.quickBar, id] };
        }),
      reconcilePluginViews: (views) =>
        set((s) => {
          const ids = new Set(views.map((v) => v.id));
          const keep = (arr: string[]) => arr.filter((x) => !x.startsWith("plugin:") || ids.has(x));
          const arrs = { left: keep(s.railOrder.left), right: keep(s.railOrder.right), bottom: keep(s.railOrder.bottom) };
          for (const v of views) {
            if (!arrs.left.includes(v.id) && !arrs.right.includes(v.id) && !arrs.bottom.includes(v.id)) arrs[v.side].push(v.id);
          }
          return { railOrder: arrs };
        }),
      setSurface: (surface) => set({ surface }),
      setRightPanel: (rightPanel) => set({ rightPanel }),
      setPluginsTab: (pluginsTab) => set({ pluginsTab }),
      setBoxTab: (boxTab) => set({ boxTab }),
      // Switching home resets to that home's first section (its own default lives in
      // SettingsSurface); callers that want a specific section call setSettingsSection too.
      setSettingsScope: (settingsScope) => set({ settingsScope }),
      setSettingsSection: (settingsSection) => set({ settingsSection }),
      setFleetStartNew: (fleetStartNew) => set({ fleetStartNew }),
      setActivityTab: (activityTab) => set({ activityTab }),
      setRightCollapsed: (rightCollapsed) => set({ rightCollapsed }),
      setLeftCollapsed: (leftCollapsed) => set({ leftCollapsed }),
      // The DS AppShell is a CONTROLLED width: during a divider drag it streams transient
      // widths from 0 up to the full left+right span (that's how a drag collapses a side —
      // the column has to track the pointer past its min/max), and it commits its OWN
      // clamped value (`clampOpen`) on pointer-up. So store the value verbatim. The old
      // [280,720] clamp here re-clamped those transients and broke the gesture: the right
      // column could never grow past 720, so the LEFT column never reached its collapse
      // threshold (left wouldn't close), and the column stopped tracking the pointer mid-drag.
      setRightWidth: (w) => set({ rightWidth: Math.max(0, Math.round(w)) }),
      setBottomPanel: (bottomPanel) => set({ bottomPanel }),
      setBottomHeight: (h) => set({ bottomHeight: Math.max(0, Math.round(h)) }),
      setBottomCollapsed: (bottomCollapsed) => set({ bottomCollapsed }),
      pluginDots: {},
      setPluginDot: (key, on) =>
        set((s) => {
          if (Boolean(s.pluginDots[key]) === on) return s; // no-op → no rerender
          const next = { ...s.pluginDots };
          if (on) next[key] = true;
          else delete next[key];
          return { pluginDots: next };
        }),
    }),
    {
      name: "protoagent.ui", // localStorage key (per-agent-suffixed in fleet mode — see _layoutStorage)
      storage: _layoutStorage,
      version: 6, // …v4 +Box · v5 Schedule→Activity tab (#1075) · v6 +bottom dock
      migrate: (persisted: unknown) => migrateUiState(persisted) as never,
    },
  ),
);
