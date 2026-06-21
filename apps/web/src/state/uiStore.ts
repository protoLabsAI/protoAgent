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
  | "chat" | "activity" | "studio" | "knowledge" | "plugins" | "settings" | (string & {});
// `notes` is no longer a built-in right panel — it's the first-party `notes` plugin
// (keyed `plugin:notes:<view>`), so it falls under the open `(string & {})` arm.
export type RightPanel = "tasks" | "goals" | (string & {}); // + plugin:<id>:<viewId>
// Two sections (ADR 0059 D4): "local" = Installed (+ advanced install-from-URL),
// "market" = Discover. (Keys kept for persisted-state compat; the old "download"
// tab is gone — a stale persisted value falls back to Installed.)
export type PluginsTab = "local" | "market";
// Settings IA (ADR 0048): scope is the primary axis — two homes, each with its own
// section sub-nav. `settingsScope` picks the home; `settingsSection` the active
// section within it (a free string so each home owns its own section ids).
export type SettingsScope = "host" | "workspace";

type UIState = {
  surface: Surface;
  rightPanel: RightPanel;
  pluginsTab: PluginsTab;
  settingsScope: SettingsScope;
  settingsSection: string;
  // One-shot: the FleetSwitcher's "+ New agent" deep-link routes to Host/App ▸ Fleet
  // and asks the fleet panel to open the new-agent picker on mount, then clears it.
  fleetStartNew: boolean;
  // Global settings overlay (the Global home; opened from the header drawer or a
  // command-palette deep-link). EPHEMERAL — partialized out of persistence so a refresh
  // never reopens it. The section deep-links a Global section (e.g. "telemetry").
  globalSettingsOpen: boolean;
  globalSettingsSection?: string;
  openGlobalSettings: (section?: string) => void;
  closeGlobalSettings: () => void;
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
  // Re-add any CORE surface missing from a persisted railOrder to its default dock. railSurfaces()
  // renders only ids already in railOrder and never re-adds a missing core surface, so a layout
  // saved before a surface existed (or that dropped one) silently loses the icon — this is the
  // general safety net (replaces the per-surface v9-style migrations). Idempotent; no-op when whole.
  reconcileCoreSurfaces: (ids: string[]) => void;
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
  setSettingsScope: (s: SettingsScope) => void;
  setSettingsSection: (s: string) => void;
  setFleetStartNew: (b: boolean) => void;
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

// The pristine rail layout — the store's initial value AND the side-of-record for
// `reconcileCoreSurfaces`, which re-adds a CORE surface to its default dock when a
// persisted `railOrder` is missing it (see the action). Keep ids in sync with
// CORE_SURFACES (apps/web/src/app/coreSurfaces.tsx).
const DEFAULT_RAIL_ORDER: { left: string[]; right: string[]; bottom: string[] } = {
  left: ["chat", "knowledge"],
  right: ["work"],
  bottom: [],
};
const coreDefaultSide = (id: string): "left" | "right" | "bottom" | null =>
  DEFAULT_RAIL_ORDER.left.includes(id)
    ? "left"
    : DEFAULT_RAIL_ORDER.right.includes(id)
      ? "right"
      : DEFAULT_RAIL_ORDER.bottom.includes(id)
        ? "bottom"
        : null;

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
    // Prune dead rail ids from a persisted railOrder (they'd linger with no surface
    // metadata): "box" folded into Settings ▸ Global (Fleet/Telemetry/Commons are
    // sections there now). ("schedule" is a live rail surface again — v7 below.)
    const ro2 = rest.railOrder as { left?: string[]; right?: string[] } | undefined;
    if (ro2 && (Array.isArray(ro2.left) || Array.isArray(ro2.right))) {
      const live = (x: string) => x !== "box";
      rest.railOrder = {
        left: (ro2.left ?? []).filter(live),
        right: (ro2.right ?? []).filter(live),
      };
    }
    // v6 (bottom dock): railOrder gains a `bottom` dock — add the empty array to a
    // persisted layout that predates it so the shape is complete.
    const ro3 = rest.railOrder as { left?: string[]; right?: string[]; bottom?: string[] } | undefined;
    if (ro3 && !Array.isArray(ro3.bottom)) {
      rest.railOrder = { ...ro3, bottom: [] };
    }
    // v7: "schedule" is a top-level rail surface again (un-fold from #1075). Re-add it
    // to a persisted layout that had it pruned/folded — after "activity" on the left —
    // unless the user already keeps it on some dock.
    const ro4 = rest.railOrder as { left?: string[]; right?: string[]; bottom?: string[] } | undefined;
    if (ro4) {
      const has = (arr?: string[]) => Array.isArray(arr) && arr.includes("schedule");
      if (!has(ro4.left) && !has(ro4.right) && !has(ro4.bottom)) {
        const left = Array.isArray(ro4.left) ? ro4.left.slice() : [];
        const at = left.indexOf("activity");
        if (at >= 0) left.splice(at + 1, 0, "schedule");
        else left.push("schedule");
        rest.railOrder = { ...ro4, left };
      }
    }
    // v8 (2026-06 IA pass): Activity is no longer a rail surface — it moved to a
    // read-only utility-bar widget (the bottom-left widgets cluster). Prune "activity"
    // from every dock + the mobile quick-bar so it doesn't linger as a dead rail id.
    // (Runs AFTER the v7 schedule re-add, which anchors on "activity" before it's gone.)
    const ro5 = rest.railOrder as { left?: string[]; right?: string[]; bottom?: string[] } | undefined;
    if (ro5) {
      const noAct = (arr?: string[]) => (Array.isArray(arr) ? arr.filter((x) => x !== "activity") : []);
      rest.railOrder = { left: noAct(ro5.left), right: noAct(ro5.right), bottom: noAct(ro5.bottom) };
    }
    // v9 (2026-06-18 IA pass): Workspace settings became a rail surface (id "settings").
    // The default railOrder gained it, but `railSurfaces()` only renders ids already in a
    // user's persisted railOrder and nothing re-adds a missing CORE surface — so anyone
    // with a layout saved before the pass lost the Settings icon entirely (only the Global
    // overlay in the header drawer remained). Re-add "settings" to the left rail (after
    // "plugins" if present, else at the end) unless the user already keeps it on some dock.
    // Mirrors the v7 schedule re-add.
    const ro6 = rest.railOrder as { left?: string[]; right?: string[]; bottom?: string[] } | undefined;
    if (ro6) {
      const has = (arr?: string[]) => Array.isArray(arr) && arr.includes("settings");
      if (!has(ro6.left) && !has(ro6.right) && !has(ro6.bottom)) {
        const left = Array.isArray(ro6.left) ? ro6.left.slice() : [];
        const at = left.indexOf("plugins");
        if (at >= 0) left.splice(at + 1, 0, "settings");
        else left.push("settings");
        rest.railOrder = { ...ro6, left };
      }
    }
    // v10 (2026-06): the Plugins manager moved off the rail into Settings ▸ Plugins (it's a
    // settings section now, not a surface). Prune "plugins" from every dock + the quick-bar.
    const ro7 = rest.railOrder as { left?: string[]; right?: string[]; bottom?: string[] } | undefined;
    if (ro7) {
      const noPlug = (arr?: string[]) => (Array.isArray(arr) ? arr.filter((x) => x !== "plugins") : []);
      rest.railOrder = { left: noPlug(ro7.left), right: noPlug(ro7.right), bottom: noPlug(ro7.bottom) };
    }
    // v11 (2026-06): Tasks + Goals + Schedule folded into the unified "work" hub. Prune the
    // three old ids from every dock + the quick-bar; add "work" to the right rail if the
    // layout has none of them placed; retarget a default-active right panel that pointed at one.
    const FOLDED = new Set(["tasks", "goals", "schedule"]);
    const ro8 = rest.railOrder as { left?: string[]; right?: string[]; bottom?: string[] } | undefined;
    if (ro8) {
      const drop = (arr?: string[]) => (Array.isArray(arr) ? arr.filter((x) => !FOLDED.has(x)) : []);
      const left = drop(ro8.left);
      let right = drop(ro8.right);
      const bottom = drop(ro8.bottom);
      if (![...left, ...right, ...bottom].includes("work")) right = [...right, "work"];
      rest.railOrder = { left, right, bottom };
    }
    if (rest.rightPanel === "tasks" || rest.rightPanel === "goals") rest.rightPanel = "work";
    // v12 (2026-06): Settings moved off the rail into a utility-bar pill (the settings
    // dialog). Prune "settings" from every dock + the quick-bar — it's no longer a surface.
    const ro9 = rest.railOrder as { left?: string[]; right?: string[]; bottom?: string[] } | undefined;
    if (ro9) {
      const noSettings = (arr?: string[]) => (Array.isArray(arr) ? arr.filter((x) => x !== "settings") : []);
      rest.railOrder = { left: noSettings(ro9.left), right: noSettings(ro9.right), bottom: noSettings(ro9.bottom) };
    }
    if (Array.isArray(rest.quickBar)) {
      rest.quickBar = (rest.quickBar as string[]).filter(
        (x) => x !== "activity" && x !== "plugins" && x !== "settings" && !FOLDED.has(x),
      );
    }
    return rest;
  }
  return persisted;
}

export const useUI = create<UIState>()(
  persist(
    (set) => ({
      surface: "chat",
      rightPanel: "work",
      pluginsTab: "local",
      settingsScope: "host" as SettingsScope,
      settingsSection: "overview",
      fleetStartNew: false,
      globalSettingsOpen: false,
      globalSettingsSection: undefined,
      openGlobalSettings: (section) => set({ globalSettingsOpen: true, globalSettingsSection: section }),
      closeGlobalSettings: () => set({ globalSettingsOpen: false }),
      rightCollapsed: false,
      leftCollapsed: false,
      rightWidth: 360,
      bottomPanel: "",
      bottomHeight: 240,
      bottomCollapsed: false,
      railOrder: { left: [...DEFAULT_RAIL_ORDER.left], right: [...DEFAULT_RAIL_ORDER.right], bottom: [...DEFAULT_RAIL_ORDER.bottom] },
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
      quickBar: ["chat", "knowledge", "plugins"],
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
      reconcileCoreSurfaces: (ids) =>
        set((s) => {
          const placed = new Set([...s.railOrder.left, ...s.railOrder.right, ...s.railOrder.bottom]);
          const missing = ids.filter((id) => !placed.has(id) && coreDefaultSide(id));
          if (!missing.length) return {}; // whole already — avoid a needless state write
          const arrs = { left: [...s.railOrder.left], right: [...s.railOrder.right], bottom: [...s.railOrder.bottom] };
          for (const id of missing) arrs[coreDefaultSide(id)!].push(id);
          return { railOrder: arrs };
        }),
      setSurface: (surface) => set({ surface }),
      setRightPanel: (rightPanel) => set({ rightPanel }),
      setPluginsTab: (pluginsTab) => set({ pluginsTab }),
      // Switching home resets to that home's first section (its own default lives in
      // SettingsSurface); callers that want a specific section call setSettingsSection too.
      setSettingsScope: (settingsScope) => set({ settingsScope }),
      setSettingsSection: (settingsSection) => set({ settingsSection }),
      setFleetStartNew: (fleetStartNew) => set({ fleetStartNew }),
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
      version: 12, // …v10 Plugins→Settings section · v11 Tasks+Goals+Schedule→Work hub · v12 Settings→utility pill (prune rail id)
      migrate: (persisted: unknown) => migrateUiState(persisted) as never,
      // The Global settings overlay is ephemeral UI state — drop it from persistence so a
      // refresh never reopens it (everything else persists as before).
      partialize: ({ globalSettingsOpen: _o, globalSettingsSection: _s, ...rest }) => rest,
    },
  ),
);
