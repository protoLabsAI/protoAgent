import { create } from "zustand";

import type { DocumentSpec } from "./types";

// The full-screen document viewer (ADR 0062) — one root-mounted host, opened imperatively
// from anywhere via `openDocument(spec)`, mirroring the context-menu system's store+host
// pattern. Ephemeral by design (never persisted): a refresh closes it.
type DocViewerState = {
  open: boolean;
  doc: DocumentSpec | null;
};

export const useDocViewer = create<DocViewerState>(() => ({ open: false, doc: null }));

/** Open the full-screen reader on `doc`. Replacing an open doc swaps content in place. */
export function openDocument(doc: DocumentSpec): void {
  useDocViewer.setState({ open: true, doc });
}

export function closeDocument(): void {
  useDocViewer.setState({ open: false, doc: null });
}
