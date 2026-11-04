import { create } from "zustand";

// The run inspector drawer (#4726): one root-mounted host (`RunInspector`), opened from
// anywhere with `openRunInspector(runId)`, the same store + host pattern as the document
// viewer (docviewer/store.ts). Ephemeral by design: a refresh closes it, and the `?run=`
// deep link is how a run is reopened or shared.
type RunInspectorState = { runId: string | null };

export const useRunInspector = create<RunInspectorState>(() => ({ runId: null }));

/** Open the run drawer on `runId` (any id the run route resolves). Swaps an open run in place. */
export function openRunInspector(runId: string): void {
  const id = (runId || "").trim();
  if (id) useRunInspector.setState({ runId: id });
}

export function closeRunInspector(): void {
  useRunInspector.setState({ runId: null });
}
