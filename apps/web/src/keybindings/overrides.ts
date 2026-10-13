import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import type { Keybinding } from "../ext/keybindingRegistry";
import { persistStorage } from "../lib/storage";

// User keybinding overrides (ADR 0063) — a GLOBAL map { bindingId → combo }, persisted to a
// single browser-storage key (NOT per-agent: your shortcuts are yours everywhere). A binding's
// effective combo is its override, else its registered default.
type OverridesState = {
  overrides: Record<string, string>;
  setBinding: (id: string, combo: string) => void;
  resetBinding: (id: string) => void;
  resetAll: () => void;
};

export const useKeybindingOverrides = create<OverridesState>()(
  persist(
    (set) => ({
      overrides: {},
      setBinding: (id, combo) => set((s) => ({ overrides: { ...s.overrides, [id]: combo } })),
      resetBinding: (id) =>
        set((s) => {
          if (!(id in s.overrides)) return s;
          const next = { ...s.overrides };
          delete next[id];
          return { overrides: next };
        }),
      resetAll: () => set({ overrides: {} }),
    }),
    {
      name: "protoagent.keybindings", // global — not suffixed per-agent
      // The storage seam (ADR 0114 D1) — without it zustand falls back to raw localStorage,
      // whose setItem throws out of every `set()` once the quota is full.
      storage: createJSONStorage(() => persistStorage("local")),
    },
  ),
);

/** The combo a binding actually responds to: the user override, else its default. */
export function effectiveCombo(b: Pick<Keybinding, "id" | "defaultKeys">): string {
  return useKeybindingOverrides.getState().overrides[b.id] ?? b.defaultKeys;
}
