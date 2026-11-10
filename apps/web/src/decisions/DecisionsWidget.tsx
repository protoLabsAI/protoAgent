import type { ReactNode } from "react";
import { MessageCircleQuestion } from "lucide-react";
import { Count } from "@protolabsai/ui/primitives";

import type { DecisionRow } from "../lib/api/decisions";
import { UtilityWidget } from "../app/UtilityWidget";
import { DecisionsPanel } from "./DecisionsPanel";
import { useDecisions } from "./useDecisions";

/**
 * The desktop/tablet entry point (#4725, §2.1): a `UtilityWidget` pill in the bottom utility
 * bar, driven by the fleet-wide {@link useDecisions} store. ALWAYS rendered so the widgets to
 * its right never shift — quiet (icon, no badge) with nothing pending and before the first load,
 * a default `Count` while something waits (never a warning colour, never "0"). The dialog body is
 * {@link DecisionsPanel}; the store owns the open state so a notification click or an Activity
 * link can open it from outside. Not mounted here — the mount card wires it into `App.tsx`.
 */

/** The dialog is deliberately narrower than the feed's — one card at a time reads better. jsdom
 *  can't round-trip a `min()` width, so the value lives here where a test can assert it directly. */
export const DECISIONS_DIALOG_WIDTH = "min(520px, 94vw)";

const QUESTION_PREVIEW_MAX = 120;

/** The pill's accessible name (§4.1) — the count is spoken inside it so a screen reader hears
 *  "Decisions, 2 waiting on you", never a bare "2". */
export function decisionsLabel(pendingCount: number): string {
  if (pendingCount <= 0) return "Decisions, nothing waiting";
  if (pendingCount === 1) return "Decisions, 1 waiting on you";
  return `Decisions, ${pendingCount} waiting on you`;
}

/** The hover/focus info (§2.1, §4.1): the top decision's agent + question (cut at 120 chars),
 *  plus an "and N−1 more" line when several wait; the quiet line otherwise. A convenience preview
 *  — everything in it is also in the dialog, so nothing is hover-only. */
export function decisionsInfo(top: DecisionRow | null, pendingCount: number): ReactNode {
  if (pendingCount <= 0 || !top) return "Decisions · nothing is waiting on you";
  const question =
    top.question.length > QUESTION_PREVIEW_MAX
      ? `${top.question.slice(0, QUESTION_PREVIEW_MAX)}…`
      : top.question;
  return (
    <>
      <span style={{ display: "block" }}>
        <strong>{top.agent || "An agent"}:</strong> {question}
      </span>
      {pendingCount > 1 ? (
        <span style={{ display: "block" }}>{`and ${pendingCount - 1} more`}</span>
      ) : null}
    </>
  );
}

export function DecisionsWidget({ onOpen }: {
  /** Shell pill navigation; other entry points retain the legacy Decisions dialog. */
  onOpen?: () => void;
} = {}) {
  const { decisions, pendingCount, top, loaded, panelOpen, openPanel, closePanel, answer } =
    useDecisions();
  const visibleCount = loaded ? pendingCount : 0;
  return (
    <UtilityWidget
      testId="decisions-widget"
      icon={<MessageCircleQuestion size={14} />}
      // A badge only while something waits — never "0", never a warning tint (§2.1).
      badge={visibleCount > 0 ? <Count>{visibleCount > 9 ? "9+" : visibleCount}</Count> : undefined}
      label={decisionsLabel(visibleCount)}
      info={decisionsInfo(top, visibleCount)}
      dialogTitle="Decisions waiting on you"
      dialogWidth={DECISIONS_DIALOG_WIDTH}
      open={panelOpen}
      onOpenChange={(next) => (next ? (onOpen ?? openPanel)() : closePanel())}
    >
      <DecisionsPanel decisions={decisions} loading={!loaded} onAnswer={answer} />
    </UtilityWidget>
  );
}
