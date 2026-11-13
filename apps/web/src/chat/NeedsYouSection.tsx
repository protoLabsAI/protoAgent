/** The "Needs you · N" section (work consolidation v2 S7) — ALWAYS the grouped details
 *  panel's first section (ChatStatusRail hosts it). Decisions reuse the shared DecisionCard +
 *  decisions store (no forked answer logic): the first one expanded, the rest one-line
 *  agent/question/age rows that expand on select. A card whose interaction settled it is HELD
 *  by the host until the panel closes (the lifted `held` map), while the heading count stays
 *  pending-only. Undelivered human-needed pages and stopped goals are neutral rows — never
 *  forced into DecisionCards — the goals with their existing Keep going / Drop the goal
 *  controls (work/sections/goals). */
import { useState } from "react";
import { Button } from "@protolabsai/ui/primitives";

import { api } from "../lib/api";
import type { GoalState, InboxItem } from "../lib/types";
import type { AnswerResult, DecisionRow } from "../lib/api/decisions";
import { DecisionCard } from "../decisions/DecisionCard";
import { openSourceChat } from "../work/openSourceChat";
import { relTime, type GroupableWorkItem } from "../work/tone";
import { chatStore } from "./chat-store";

/** The kinds this section draws rows for — the rail (ChatStatusRail) counts EXACTLY this
 *  set, so "Needs you · N" can never claim a row the panel doesn't show. A `waiting` task
 *  (ADR 0129 Q1) rides Work's own Needs-you group instead: it has no rail row, so it must
 *  not raise the fleet-wide marker either. */
export const NEEDS_YOU_SECTION_KINDS: ReadonlySet<GroupableWorkItem["kind"]> =
  new Set(["decision", "delivery", "goal"]);

/** A decision pending this long gets the muted stale hint — never a warning callout. */
export const STALE_DECISION_MS = 30 * 60_000;
export function decisionStale(createdAt: string, now: number = Date.now()): boolean {
  const asked = new Date(createdAt).getTime();
  return Number.isFinite(asked) && now - asked >= STALE_DECISION_MS;
}

export function NeedsYouSection({ items, held, onHold, records, answer, mobile, onNavigate, openWork, goalsRetry }: {
  /** The shared selector's pending Needs-you rows (decisions, deliveries, stopped goals). */
  items: GroupableWorkItem[];
  /** Interacted decisions the host retains while the panel is open (§5.5 hold). */
  held: Map<string, DecisionRow>;
  onHold: (row: DecisionRow) => void;
  /** The shared store's by-id records — a held card keeps showing its freshest state. */
  records: Record<string, DecisionRow>;
  answer: (id: string, optionId: string) => Promise<AnswerResult>;
  mobile: boolean;
  onNavigate: () => void;
  /** Deep-link to the Needs-you group in Work — UNFILTERED by design (never chat-scoped). */
  openWork: () => void;
  /** Refetch the goals source after Keep going / Drop the goal. */
  goalsRetry: () => void;
}) {
  const size = mobile ? "md" : "sm";
  // One-way per open panel: a row expands on select; the first decision starts expanded.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  // Inline notes (the popover has no toast host): a failed goal action or chat open says so
  // in place, muted — never a warning callout.
  const [note, setNote] = useState<string | null>(null);
  const [goalBusy, setGoalBusy] = useState<string | null>(null);
  const [confirmDrop, setConfirmDrop] = useState<string | null>(null);

  const decisions = items.filter((item) => item.kind === "decision");
  const deliveries = items.filter((item) => item.kind === "delivery");
  const goals = items.filter((item) => item.kind === "goal");
  const count = items.length;
  const live = new Set(decisions.map((item) => item.id));
  // ONE list, keyed by id (the DecisionsPanel shape): a held card that leaves the pending
  // list keeps its React identity — and so its "Sent" result — instead of remounting.
  const rows: DecisionRow[] = [
    ...decisions.map((item) => item.raw as DecisionRow),
    ...[...held.values()].filter((row) => !live.has(row.id)),
  ].map((row) => records[row.id] ?? row);

  const openChat = async (row: DecisionRow, reply: boolean) => {
    if (await openSourceChat(row.session_id, reply)) onNavigate();
    else setNote("Couldn't open that chat. Try again when the agent is connected.");
  };
  // "Open chat: {title}" when the source chat is already hydrated; the card falls back to
  // the agent name otherwise (the same contract the Decisions panel's chatTitleFor carries).
  const chatTitle = (row: DecisionRow) =>
    chatStore.getSnapshot().sessions.find((session) => session.id === row.session_id)?.title || undefined;
  const goalAction = async (id: string, run: () => Promise<unknown>) => {
    setGoalBusy(id);
    setNote(null);
    try {
      await run();
      goalsRetry();
    } catch {
      setNote("That didn't go through. Nothing changed — you can try again.");
    } finally {
      setGoalBusy(null);
      setConfirmDrop(null);
    }
  };

  const name = count > 0 ? `Needs you, ${count}` : "Needs you";
  return (
    <section className="chat-status-details__section chat-needs-you" aria-label={name}>
      <h2 className="chat-status-details__heading" aria-label={name}>
        Needs you{count > 0 && <span aria-hidden="true"> · {count}</span>}
      </h2>
      {note && <p className="chat-status-details__problem" role="presentation">{note}</p>}
      {rows.map((row, index) =>
        index === 0 || expanded.has(row.id) || held.has(row.id) ? (
          <div key={row.id} className="chat-needs-you__card">
            <DecisionCard
              decision={row}
              actionSize={size}
              onAnswer={(optionId) => answer(row.id, optionId)}
              onHold={onHold}
              onOpenChat={(d) => void openChat(d, false)}
              onReplyInChat={(d) => void openChat(d, true)}
              chatTitle={chatTitle(row)}
            />
            {row.state === "pending" && decisionStale(row.created_at) && (
              <p className="chat-needs-you__stale">Still waiting — asked over 30 minutes ago.</p>
            )}
          </div>
        ) : (
          <button
            key={row.id}
            type="button"
            className="chat-needs-you__row"
            aria-expanded={false}
            onClick={() => setExpanded((prev) => new Set(prev).add(row.id))}
          >
            <span className="chat-status-details__title" title={row.question}>
              <strong>{row.agent || "An agent"}</strong> · {row.question}
            </span>
            <span className="chat-status-details__meta" title={relTime(row.created_at).tooltip}>
              {relTime(row.created_at).text}
            </span>
          </button>
        ))}
      {(deliveries.length > 0 || goals.length > 0) && (
        <ul className="chat-status-details__rows">
          {deliveries.map((item) => (
            <li key={`delivery:${item.id}`} className="chat-status-details__row">
              <span className="chat-status-details__title" title={item.title}>{item.title}</span>
              <span className="chat-status-details__meta">
                {[(item.raw as InboxItem).source, "couldn't be delivered"].filter(Boolean).join(" · ")}
              </span>
            </li>
          ))}
          {goals.map((item) => {
            const goal = item.raw as GoalState;
            return (
              <li key={`goal:${item.id}`} className="chat-status-details__row chat-needs-you__goal">
                <span className="chat-status-details__title" title={item.title}>
                  <strong>Goal stopped</strong> · {item.title}
                </span>
                <span className="chat-needs-you__goal-actions">
                  {confirmDrop === item.id ? (
                    <>
                      <span className="chat-status-details__meta">Drop this goal?</span>
                      <Button size={size} variant="ghost" disabled={goalBusy === item.id}
                        onClick={() => void goalAction(item.id, () => api.clearGoal(goal.session_id, false))}>
                        Drop the goal
                      </Button>
                      <Button size={size} variant="ghost" disabled={goalBusy === item.id}
                        onClick={() => setConfirmDrop(null)}>
                        Keep it
                      </Button>
                    </>
                  ) : (
                    <>
                      <Button size={size} variant="ghost" disabled={goalBusy === item.id}
                        onClick={() => void goalAction(item.id, () => api.rearmGoal(goal.session_id, {}))}>
                        Keep going
                      </Button>
                      <Button size={size} variant="ghost" disabled={goalBusy === item.id}
                        onClick={() => setConfirmDrop(item.id)}>
                        Drop the goal
                      </Button>
                    </>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      <div className="chat-status-details__actions">
        <Button
          size={size}
          variant="ghost"
          onClick={() => {
            onNavigate();
            openWork();
          }}
        >
          Open in Work
        </Button>
      </div>
    </section>
  );
}
