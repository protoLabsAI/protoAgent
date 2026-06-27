import { create } from "zustand";

// Ephemeral bridge (ADR 0063) for keybindings whose action needs app/React context rather
// than a store call. A binding pokes an intent here; the owning component reacts:
//   • paletteOpen        — App drives <CommandPalette open=…> from this (⌘K adopted off the DS hook)
//   • composerFocusNonce — the visible chat session slot focuses its composer when this bumps
// NOT persisted.
type KbIntents = {
  paletteOpen: boolean;
  setPaletteOpen: (open: boolean) => void;
  togglePalette: () => void;
  composerFocusNonce: number;
  focusComposer: () => void;
  // True while the settings UI is recording a new shortcut — the global host bails so the
  // keys being captured don't also fire their current binding.
  capturing: boolean;
  setCapturing: (capturing: boolean) => void;
};

export const useKbIntents = create<KbIntents>((set) => ({
  paletteOpen: false,
  setPaletteOpen: (paletteOpen) => set({ paletteOpen }),
  togglePalette: () => set((s) => ({ paletteOpen: !s.paletteOpen })),
  composerFocusNonce: 0,
  focusComposer: () => set((s) => ({ composerFocusNonce: s.composerFocusNonce + 1 })),
  capturing: false,
  setCapturing: (capturing) => set({ capturing }),
}));
