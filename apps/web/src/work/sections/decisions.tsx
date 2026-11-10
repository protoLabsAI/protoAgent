/**
 * The Decisions work section (#4955 §2.4) — the list rows and the per-item detail for the
 * agent's "decision needed" ledger, registered into the Work view on import.
 *
 * The row follows §2.4: the question is the title (wrapping to two lines, the shared WorkRow
 * rule), the status badge comes from the §2.2 tone map (pending → needs you, answered → done,
 * expired/cancelled/withdrawn → cancelled), and the meta line reads "Asked {time}" while
 * pending, "Answered: {label} · by {who} · {time}" once answered, and "Withdrawn: {reason}"
 * when closed without an answer. The agent name is shown ONLY in the agent-wide ("All chats")
 * filter. No id, session id, thread id or interrupt id is ever put in a row (§2.3, §2.4).
 *
 * Pending decisions sort first, so the section mirrors the Overview's "Needs you, pinned
 * first" rule (§2.1, Amendment A1 — placement only). The detail is the existing DecisionCard
 * (docs/design/decision-needed.md, revision 1), rendered unchanged; it shows none of the
 * technical ids, so §2.4's "never shown" list holds for free.
 */
import type { ReactNode } from "react";
import { Empty } from "@protolabsai/ui/primitives";

import { decisionsApi, type DecisionRow } from "../../lib/api/decisions";
import { DecisionCard } from "../../decisions/DecisionCard";
import { relTime } from "../tone";
import { WorkRow } from "../WorkRow";
import type { WorkItem } from "../useWorkItems";
import { registerWorkSection, type WorkSectionContext } from "../WorkView";

/** Who answered, in plain words: the operator is "you"; otherwise the agent/name as given. */
function answeredBy(row: DecisionRow): string {
  if (!row.answered_by) return "";
  return row.answered_by === "operator" || row.answered_by === "console" ? "you" : row.answered_by;
}

/** The single meta line under a decision row (§2.4). Never carries an id of any kind. */
export function decisionMeta(row: DecisionRow, allChats: boolean): string {
  const parts: string[] = [];
  // §2.4 field 5: the agent only shows in the agent-wide filter.
  if (allChats && row.agent) parts.push(row.agent);
  if (row.state === "pending") {
    const asked = relTime(row.created_at).text;
    if (asked) parts.push(`Asked ${asked}`);
  } else if (row.state === "answered") {
    const label = row.answer_label ?? row.answer ?? "an option";
    const who = answeredBy(row);
    const when = relTime(row.answered_at ?? row.created_at).text;
    parts.push(`Answered: ${label}${who ? ` · by ${who}` : ""}${when ? ` · ${when}` : ""}`);
  } else {
    // expired / cancelled / withdrawn — all map to the "cancelled" badge (§2.4 field 2).
    const reason = row.withdraw_reason?.trim();
    if (reason) parts.push(`Withdrawn: ${reason}`);
  }
  return parts.join(" · ");
}

/** The selected decision's detail: the DecisionCard, unchanged. */
function DecisionDetail({ row }: { row: DecisionRow }): ReactNode {
  return (
    <div className="work-decision-detail">
      <DecisionCard
        decision={row}
        onAnswer={(optionId) => decisionsApi.answerDecision(row.id, { option_id: optionId })}
      />
    </div>
  );
}

function renderDecisions(ctx: WorkSectionContext): ReactNode {
  const items = ctx.sources.decisions.items;

  // Detail: the one decision whose id the host put in state.itemId (§4.1 push navigation).
  if (ctx.state.itemId) {
    const selected = items.find((item) => `${item.kind}:${item.id}` === ctx.state.itemId);
    if (selected) return <DecisionDetail row={selected.raw as DecisionRow} />;
  }

  if (!items.length) {
    return (
      <Empty
        title="No decisions to show"
        description="When an agent stops to ask you something, it appears here."
      />
    );
  }

  // Pending (needs-you) first, pinned like the Overview's Needs you lane; the rest keep the
  // server's soonest-expiry-first order.
  const pending = items.filter((item) => item.status === "needs-you");
  const closed = items.filter((item) => item.status !== "needs-you");
  const allChats = ctx.state.filters?.thisChat === false;

  const rowFor = (item: WorkItem) => {
    const row = item.raw as DecisionRow;
    return (
      <WorkRow
        key={`${item.kind}:${item.id}`}
        status={item.status}
        title={row.question}
        meta={decisionMeta(row, allChats)}
        focusKey={`${item.kind}:${item.id}`}
        onOpen={() => ctx.onOpen(item)}
      />
    );
  };

  return <div className="work-section work-section--decisions">{[...pending, ...closed].map(rowFor)}</div>;
}

registerWorkSection("decisions", { label: "Decisions", render: renderDecisions });
