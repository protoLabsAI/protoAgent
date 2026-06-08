import type * as React from "react";
import { create } from "zustand";

import type { ContextType } from "./types";

interface ContextMenuState {
  open: boolean;
  type: ContextType;
  x: number;
  y: number;
  ctx: unknown;
  openMenu: (type: ContextType, x: number, y: number, ctx?: unknown) => void;
  close: () => void;
}

export const useContextMenuStore = create<ContextMenuState>((set) => ({
  open: false, type: "", x: 0, y: 0, ctx: undefined,
  openMenu: (type, x, y, ctx) => set({ open: true, type, x, y, ctx }),
  close: () => set({ open: false }),
}));

// Imperative open-at-cursor (ADR 0036 D1) — call from any onContextMenu handler.
export function openContextMenu(type: ContextType, e: React.MouseEvent, ctx?: unknown) {
  e.preventDefault();
  useContextMenuStore.getState().openMenu(type, e.clientX, e.clientY, ctx);
}
