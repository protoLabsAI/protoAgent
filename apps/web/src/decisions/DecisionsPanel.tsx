import { type CSSProperties, Fragment, useRef, useState } from "react";

import { Divider } from "@protolabsai/ui/primitives";
import { ScrollArea, Skeleton } from "@protolabsai/ui/data";
import { Drawer } from "@protolabsai/ui/overlays";

import type { AnswerResult, DecisionRow } from "../lib/api/decisions";
import { DecisionCard } from "./DecisionCard";

// Tokens only (§5.10).
const body: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--pl-space-4)" };
const intro: CSSProperties = { color: "var(--pl-color-fg-muted)", margin: 0 };
const empty: CSSProperties = { color: "var(--pl-color-fg-muted)", margin: 0 };
const skeletonStack: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--pl-space-4)" };

export type DecisionsPanelProps = {
  /** Pending decisions in server order (from `useDecisions`). */
  decisions: DecisionRow[];
  /** First load hasn't resolved — show the skeleton (§5.2). */
  loading: boolean;
  /** Answer one decision (card id + option id). */
  onAnswer: (id: string, optionId: string) => Promise<AnswerResult>;
  onOpenChat?: (decision: DecisionRow) => void;
  onReplyInChat?: (decision: DecisionRow) => void;
  /** Presence lookup by agent name; defaults to online. */
  agentOnline?: (agent: string) => boolean;
  agentHoldsOffline?: boolean;
  onStartAgent?: (decision: DecisionRow) => void;
  chatTitleFor?: (decision: DecisionRow) => string | undefined;
};

const INTRO = "Your agents stopped to ask you these. Pick an answer and the agent carries on.";
const NOTHING =
  "Nothing is waiting on you. When an agent stops to ask you something, it shows up here.";
const EVERYTHING = "That's everything. Nothing is waiting on you.";

/**
 * The Decisions panel BODY (#4725, §3–§4.2) — intro line, the pending cards in server order
 * with a `Divider` between them, the loading skeleton, and the two empty-state lines. No
 * `Popover` (revision 1 dropped it). Rendered inside the `UtilityWidget` dialog on desktop and
 * inside {@link DecisionsSheet} on the phone; both are wired by the mount card.
 */
export function DecisionsPanel({
  decisions,
  loading,
  onAnswer,
  onOpenChat,
  onReplyInChat,
  agentOnline,
  agentHoldsOffline,
  onStartAgent,
  chatTitleFor,
}: DecisionsPanelProps) {
  // Retain a card through its "Sent" hold so it never vanishes under the pointer (§5.5): a card
  // reports `onHold` when it starts sending and `onResolved` when its 4-second hold elapses. The
  // store list is the live truth; a held id that has left the list stays until it resolves. The
  // held set resets naturally when the panel closes (the body unmounts).
  const [held, setHeld] = useState<Map<string, DecisionRow>>(() => new Map());
  const everShown = useRef(false);

  const live = new Set(decisions.map((d) => d.id));
  // Live cards first (server order), then any held card that has left the live list — it stays
  // showing its "Sent" hold or closed callout until it resolves or the panel closes.
  const list = [...decisions, ...[...held.values()].filter((d) => !live.has(d.id))];
  if (list.length > 0) everShown.current = true;

  const hold = (decision: DecisionRow) =>
    setHeld((prev) => new Map(prev).set(decision.id, decision));
  const resolve = (id: string) =>
    setHeld((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Map(prev);
      next.delete(id);
      return next;
    });

  if (loading && list.length === 0) {
    return (
      <div style={skeletonStack} data-testid="decisions-loading">
        <Skeleton lines={3} />
        <Skeleton lines={3} />
      </div>
    );
  }

  if (list.length === 0) {
    return <p style={empty}>{everShown.current ? EVERYTHING : NOTHING}</p>;
  }

  return (
    <div style={body}>
      <p style={intro}>{INTRO}</p>
      {list.map((decision, i) => (
        <Fragment key={decision.id}>
          {i > 0 && <Divider />}
          <DecisionCard
            decision={decision}
            onAnswer={(optionId) => onAnswer(decision.id, optionId)}
            onHold={hold}
            onResolved={resolve}
            onOpenChat={onOpenChat}
            onReplyInChat={onReplyInChat}
            chatTitle={chatTitleFor?.(decision)}
            agentOnline={agentOnline ? agentOnline(decision.agent) : undefined}
            agentHoldsOffline={agentHoldsOffline}
            onStartAgent={onStartAgent}
          />
        </Fragment>
      ))}
    </div>
  );
}

export type DecisionsSheetProps = DecisionsPanelProps & {
  open: boolean;
  onClose: () => void;
};

/**
 * The phone entry's panel: a bottom `Drawer` (§3, up to the DS 85dvh cap) titled "Decisions
 * waiting on you", wrapping {@link DecisionsPanel} in a `ScrollArea`. The mount card opens it
 * from the header button.
 */
export function DecisionsSheet({ open, onClose, ...panel }: DecisionsSheetProps) {
  return (
    <Drawer open={open} onClose={onClose} side="bottom" title="Decisions waiting on you">
      <ScrollArea ariaLabel="Decisions waiting on you">
        <DecisionsPanel {...panel} />
      </ScrollArea>
    </Drawer>
  );
}
