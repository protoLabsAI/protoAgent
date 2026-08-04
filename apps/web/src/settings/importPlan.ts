// Snapshot import-plan presentation (ADR 0091 D3, #2106) — extracted from
// ImportSnapshotPanel so the wording logic is unit-testable without mounting anything
// (this repo tests extracted helpers, not components).

import type { SnapshotImportPlan, SnapshotSecret } from "../lib/types";

/** Credentials the operator should be asked for.
 *
 *  Only ones the SOURCE agent actually had (`was_set`). A merely-declared credential — a
 *  plugin lists it, nobody ever filled it in — would put an empty field in front of the
 *  operator implying their import is incomplete when it is in exactly the state the
 *  original was in. Same rule the CLI and the import result use. */
export function neededSecrets(plan: SnapshotImportPlan | null): SnapshotSecret[] {
  return (plan?.required_secrets ?? []).filter((s) => s.was_set);
}

/** One line describing what the snapshot contains, for above the plan. */
export function planSummary(plan: SnapshotImportPlan): string {
  const bits: string[] = [];
  bits.push(plan.has_soul ? "a persona" : "no persona");
  if (plan.plugins.length) bits.push(`${plan.plugins.length} plugin${plan.plugins.length === 1 ? "" : "s"}`);
  if (plan.skill_files) bits.push(`${plan.skill_files} skill file${plan.skill_files === 1 ? "" : "s"}`);
  if (plan.mcp_servers.length) bits.push(`${plan.mcp_servers.length} MCP server${plan.mcp_servers.length === 1 ? "" : "s"}`);
  return `Contains ${bits.join(", ")}. A snapshot creates a FRESH agent — no conversation history is carried.`;
}

/** The action button's label.
 *
 *  When the snapshot installs code, the label SAYS SO — the consent has to live on the
 *  control that performs the action, not only in a paragraph above it that can be scrolled
 *  past. With no plugins there is nothing to consent to, so the label stays plain. */
export function importButtonLabel(plan: SnapshotImportPlan): string {
  const n = plan.plugins.length;
  if (!n) return "Create agent";
  return `Install ${n} plugin${n === 1 ? "" : "s"} and create agent`;
}
