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
import { useWorkActionSize } from "../../shell/SurfaceFrame";
import type { ReactNode } from "react";
import { WorkEmpty as Empty } from "../WorkEmpty";

import { type DecisionRow } from "../../lib/api/decisions";
import { DecisionCard } from "../../decisions/DecisionCard";
import { decisionMeta } from "../tone";
import { WorkRow } from "../WorkRow";
import type { WorkItem } from "../useWorkItems";
import { registerWorkSection, type WorkSectionContext } from "../sectionRegistry";

/** The selected decision's detail: the DecisionCard, unchanged. */
function DecisionDetail({ row, context }: { row: DecisionRow; context: NonNullable<WorkSectionContext["decisionActions"]> }): ReactNode {
  const actionSize = useWorkActionSize();
  return (
    <div className="work-decision-detail">
      <DecisionCard
        actionSize={actionSize}
        decision={row}
        onAnswer={(optionId) => context.answerDecision(row.id, optionId)}
        agentOnline={context.agentOnline}
        onOpenChat={() => context.openSourceChat(row.session_id)}
        onReplyInChat={() => context.openSourceChat(row.session_id, true)}
      />
    </div>
  );
}

function renderDecisions(ctx: WorkSectionContext): ReactNode {
  const items = ctx.sources.decisions.items;

  // Detail: the one decision whose id the host put in state.itemId (§4.1 push navigation).
  if (ctx.state.itemId) {
    const selected = items.find((item) => `${item.kind}:${item.id}` === ctx.state.itemId);
    if (selected && ctx.decisionActions) return <DecisionDetail key={selected.id} row={selected.raw as DecisionRow} context={ctx.decisionActions} />;
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
  // Work always lists every chat's decisions (Josh 2026-11-11), so rows name their agent.
  const allChats = true;

  const rowFor = (item: WorkItem) => {
    const row = item.raw as DecisionRow;
    return (
      <li key={`${item.kind}:${item.id}`}><WorkRow
        status={item.status}
        title={row.question}
        meta={decisionMeta(row, allChats)}
        focusKey={`${item.kind}:${item.id}`}
        onOpen={() => ctx.onOpen(item)}
      /></li>
    );
  };

  return <ul className="work-group">{[...pending, ...closed].map(rowFor)}</ul>;
}

registerWorkSection("decisions", { label: "Decisions", render: renderDecisions });
