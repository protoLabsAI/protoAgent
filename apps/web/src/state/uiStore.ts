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
import { persist } from "zustand/middleware";

import type { SettingsTab } from "../settings/SettingsSurface";

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
  rightWidth: number;
  setSurface: (s: Surface) => void;
  setRightPanel: (p: RightPanel) => void;
  setAgentTab: (t: AgentTab) => void;
  setPluginsTab: (t: PluginsTab) => void;
  setKnowledgeTab: (t: KnowledgeTab) => void;
  setSettingsTab: (t: SettingsTab) => void;
  setActivityTab: (t: ActivityTab) => void;
  setRightCollapsed: (b: boolean) => void;
  setRightWidth: (w: number) => void;
};

export const useUI = create<UIState>()(
  persist(
    (set) => ({
      surface: "chat",
      rightPanel: "notes",
      agentTab: "identity",
      pluginsTab: "local",
      knowledgeTab: "store",
      settingsTab: "overview" as SettingsTab,
      activityTab: "thread",
      rightCollapsed: false,
      rightWidth: 360,
      setSurface: (surface) => set({ surface }),
      setRightPanel: (rightPanel) => set({ rightPanel }),
      setAgentTab: (agentTab) => set({ agentTab }),
      setPluginsTab: (pluginsTab) => set({ pluginsTab }),
      setKnowledgeTab: (knowledgeTab) => set({ knowledgeTab }),
      setSettingsTab: (settingsTab) => set({ settingsTab }),
      setActivityTab: (activityTab) => set({ activityTab }),
      setRightCollapsed: (rightCollapsed) => set({ rightCollapsed }),
      setRightWidth: (w) => set({ rightWidth: clampWidth(w) }),
    }),
    {
      name: "protoagent.ui", // localStorage key — the single source of truth (ADR 0035 D5)
      version: 1,
    },
  ),
);
