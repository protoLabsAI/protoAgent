import type { ReactNode } from "react";

import type { SystemNoteTone } from "../lib/types";

// Build-time fork seam for COMPOSER ACTIONS (ADR 0061, extends ADR 0038 D3). A fork drops
// a `src/ext/<name>.tsx` that calls `registerComposerAction()` to add a control to the chat
// composer's actions slot (beside the model picker) — WITHOUT editing `ChatSurface.tsx`, so
// `git pull upstream` stays conflict-free. Sibling of `registerSlashCommand` /
// `registerSurface`: static registration at module load, first-wins (HMR-safe).
//
// This is an ADDITIVE seam — core's composer controls (attach, model select, send) are DS
// PromptInput built-ins, not migrated; the registry is purely for fork-added actions.

/** What a composer action's handler receives — the host (ChatSurface) builds it. */
export type ComposerActionContext = {
  /** The active chat session id, or null. */
  sessionId: string | null;
  /** Replace the composer draft text (e.g. insert a template). */
  setDraft: (text: string) => void;
  /** Return focus to the composer textarea. */
  focusComposer: () => void;
  /** Drop a LOCAL system note into the thread (shown to the operator, never sent). `tone`
   *  colours it (info/warning/danger/success); omit for a neutral note. */
  noteToThread: (markdown: string, opts?: { tone?: SystemNoteTone }) => void;
};

export type ComposerAction = {
  /** Stable id (dedup key). */
  id: string;
  /** aria-label + tooltip for the button. */
  label: string;
  /** The button glyph (e.g. a lucide icon element). */
  icon: ReactNode;
  /** Invoked on click. */
  run: (ctx: ComposerActionContext) => void;
};

const _actions: ComposerAction[] = [];

/** Register a composer action. First registration of an id wins (HMR-safe). */
export function registerComposerAction(action: ComposerAction): void {
  const id = (action?.id || "").trim();
  if (!id || typeof action.run !== "function") return;
  if (_actions.some((a) => a.id === id)) return; // first wins
  _actions.push(action);
}

export function registeredComposerActions(): ComposerAction[] {
  return _actions;
}
