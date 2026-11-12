import type { AnswerResult } from "../lib/api/decisions";
import type { ReactNode } from "react";
import type { SurfaceState } from "../shell/surfaceState";
import type { WorkItem, WorkItems } from "./useWorkItems";
import type { WorkGroupId } from "./tone";

export type WorkSectionContext = {
  sources: WorkItems;
  state: SurfaceState;
  onOpen: (item: WorkItem) => void;
  onBack: () => void;
  /** The focused chat, for the per-session sections (the directive ledger). */
  sessionId?: string;
  decisionActions?: {
    agentOnline: boolean;
    openSourceChat: (sessionId: string, reply?: boolean) => void;
    answerDecision: (id: string, optionId: string) => Promise<AnswerResult>;
  };
};
export type WorkSection = {
  label: string;
  /** Default placement for extensions, never an override of workGroups/groupFor's
   * per-row classification. Optional during migration; required in S4c. */
  group?: WorkGroupId;
  render: (context: WorkSectionContext) => ReactNode;
};
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

export const getSnapshot = () => version;
