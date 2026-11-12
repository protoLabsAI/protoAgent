import type { AnswerResult } from "../lib/api/decisions";
import type { ReactNode } from "react";
import type { SurfaceState } from "../shell/surfaceState";
import type { WorkItem, WorkItems } from "./useWorkItems";
import { WORK_GROUP_IDS, type WorkGroupId } from "./tone";

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
  /** The canonical Work group this section's rows belong to — REQUIRED (S4c). Default
   * placement for extensions, never an override of workGroups/groupFor's per-row
   * classification. */
  group: WorkGroupId;
  render: (context: WorkSectionContext) => ReactNode;
};
export const sections = new Map<string, WorkSection>();
const listeners = new Set<() => void>();
let version = 0;
/** Register a section's list/detail renderer; the returned disposer supports plugin teardown.
 *  The group is the type contract, and untyped (plugin/JS) callers hit the same boundary at
 *  runtime: an ungrouped or unknown-group registration is rejected, never half-installed. */
export function registerWorkSection(id: string, section: WorkSection): () => void {
  if (!(WORK_GROUP_IDS as readonly string[]).includes(section.group)) {
    throw new TypeError(
      `registerWorkSection("${id}"): "group" must be one of ${WORK_GROUP_IDS.join(", ")} — got ${JSON.stringify(section.group)}`,
    );
  }
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
