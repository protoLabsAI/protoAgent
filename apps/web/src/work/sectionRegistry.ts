import type { ReactNode } from "react";
import type { SurfaceState } from "../shell/surfaceState";
import type { WorkItem, WorkItems } from "./useWorkItems";

export type WorkSectionContext = {
  sources: WorkItems;
  state: SurfaceState;
  onOpen: (item: WorkItem) => void;
  onBack: () => void;
};
type WorkSection = { label: string; render: (context: WorkSectionContext) => ReactNode };
export const sections = new Map<string, WorkSection>();
const listeners = new Set<() => void>();
let version = 0;
/** Register a section's list/detail renderer; the returned disposer supports plugin teardown. */
export function registerWorkSection(id: string, section: WorkSection): () => void {
  sections.set(id, section);
  version++;
  listeners.forEach((notify) => notify());
  return () => {
    if (sections.get(id) !== section) return;
    sections.delete(id);
    version++;
    listeners.forEach((notify) => notify());
  };
}
export const subscribe = (notify: () => void) => { listeners.add(notify); return () => { listeners.delete(notify); }; };

export const getVersion = () => version;
