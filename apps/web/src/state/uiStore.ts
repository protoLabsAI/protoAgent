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

import type { SettingsTab } from "../settings/SettingsSurface";

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
export type Surface =
  | "chat" | "activity" | "studio" | "knowledge" | "agent" | "plugins" | "settings" | (string & {});
export type RightPanel = "notes" | "beads" | "goals" | "schedule" | (string & {}); // + plugin:<id>:<viewId>
export type AgentTab = "identity" | "settings" | "tools" | "mcp" | "subagents" | "skills" | "middleware";
export type PluginsTab = "local" | "market" | "download";
export type KnowledgeTab = "store" | "settings";
export type ActivityTab = "thread" | "inbox";

const RIGHT_MIN = 280;
const RIGHT_MAX = 720;
const clampWidth = (w: number) => Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, Math.round(w)));

type UIState = {
  surface: Surface;
  rightPanel: RightPanel;
  agentTab: AgentTab;
  pluginsTab: PluginsTab;
  knowledgeTab: KnowledgeTab;
  settingsTab: SettingsTab;
  activityTab: ActivityTab;
  rightCollapsed: boolean;
  leftCollapsed: boolean;
  rightWidth: number;
  // Ordered surface lists per rail (ADR 0035 D2 + 0036) — a surface is on exactly one side, at a
  // position. Core surfaces seeded below; plugin views append by their manifest `placement`. Chat
  // is pinned left (mounts unconditionally for streaming continuity) — never moved across rails.
  railOrder: { left: string[]; right: string[] };
  moveSurface: (id: string, side: "left" | "right") => void; // splice out → append to side's bottom
  reorderSurface: (id: string, dir: -1 | 1) => void; // swap with the neighbour within its rail
  setRailOrder: (next: { left: string[]; right: string[] }) => void; // DS AppShell DnD — whole new order
  // Sync plugin views into railOrder (ADR 0036) — append newly-available ones to their placement
  // side, prune `plugin:` ids no longer present. Core surfaces are left untouched.
  reconcilePluginViews: (views: { id: string; side: "left" | "right" }[]) => void;
  // Mobile shell (ADR 0035 S4): one active surface + a configurable bottom quick-bar.
  mobileActive: string;
  setMobileActive: (id: string) => void;
  quickBar: string[]; // surfaces pinned to the mobile bottom bar (cap 5)
  toggleQuickBar: (id: string) => void;
  setSurface: (s: Surface) => void;
  setRightPanel: (p: RightPanel) => void;
  setAgentTab: (t: AgentTab) => void;
  setPluginsTab: (t: PluginsTab) => void;
  setKnowledgeTab: (t: KnowledgeTab) => void;
  setSettingsTab: (t: SettingsTab) => void;
  setActivityTab: (t: ActivityTab) => void;
  setRightCollapsed: (b: boolean) => void;
  setLeftCollapsed: (b: boolean) => void;
  setRightWidth: (w: number) => void;
  // Notification dots (ADR 0039) — a plugin surface key (`plugin:<id>:<view>`) with unseen
  // bus activity shows a rail dot until opened. Persisted so the dot survives a refresh.
  pluginDots: Record<string, boolean>;
  setPluginDot: (key: string, on: boolean) => void;
};

/** persist v1→v2 migration: drop the obsolete `railOf` (side map); `railOrder`
 * falls back to the default via the store's merge. Exported for unit testing. */
export function migrateUiState(persisted: unknown): unknown {
  if (persisted && typeof persisted === "object") {
    const { railOf: _drop, ...rest } = persisted as Record<string, unknown>;
    return rest;
  }
  return persisted;
}

export const useUI = create<UIState>()(
  persist(
    (set) => ({
      surface: "chat",
      rightPanel: "beads",
      agentTab: "identity",
      pluginsTab: "local",
      knowledgeTab: "store",
      settingsTab: "overview" as SettingsTab,
      activityTab: "thread",
      rightCollapsed: false,
      leftCollapsed: false,
      rightWidth: 360,
      railOrder: {
        left: ["chat", "activity", "studio", "knowledge", "agent", "plugins", "settings"],
        right: ["beads", "goals", "schedule"],
      },
      moveSurface: (id, side) =>
        set((s) => {
          const left = s.railOrder.left.filter((x) => x !== id);
          const right = s.railOrder.right.filter((x) => x !== id);
          (side === "left" ? left : right).push(id); // append to the target's bottom
          return { railOrder: { left, right } };
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
          return { railOrder: { left: swap(s.railOrder.left), right: swap(s.railOrder.right) } };
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
          const left = keep(s.railOrder.left);
          const right = keep(s.railOrder.right);
          for (const v of views) {
            if (!left.includes(v.id) && !right.includes(v.id)) (v.side === "left" ? left : right).push(v.id);
          }
          return { railOrder: { left, right } };
        }),
      setSurface: (surface) => set({ surface }),
      setRightPanel: (rightPanel) => set({ rightPanel }),
      setAgentTab: (agentTab) => set({ agentTab }),
      setPluginsTab: (pluginsTab) => set({ pluginsTab }),
      setKnowledgeTab: (knowledgeTab) => set({ knowledgeTab }),
      setSettingsTab: (settingsTab) => set({ settingsTab }),
      setActivityTab: (activityTab) => set({ activityTab }),
      setRightCollapsed: (rightCollapsed) => set({ rightCollapsed }),
      setLeftCollapsed: (leftCollapsed) => set({ leftCollapsed }),
      setRightWidth: (w) => set({ rightWidth: clampWidth(w) }),
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
      version: 2, // v2: railOf (side map) → railOrder (ordered lists per rail)
      migrate: (persisted: unknown) => migrateUiState(persisted) as never,
    },
  ),
);
