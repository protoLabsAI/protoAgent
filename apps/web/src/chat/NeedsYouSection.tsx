/** The "Needs you · N" section (work consolidation v2 S7, restyled into the §1 quiet frame by
 *  S3a) — ALWAYS the grouped details panel's first section (ChatStatusRail hosts it). Decisions
 *  reuse the shared DecisionCard + decisions store (no forked answer logic): the first one
 *  expanded, the rest quiet rows (the question over a "<agent> · asked <age>" line) that expand
 *  into a card on select. A card whose interaction settled it is HELD by the host until the
 *  panel closes (the lifted `held` map), while the heading count stays pending-only. Waiting
 *  tasks, undelivered human-needed pages and stopped goals are quiet rows too, reusing the S2b
 *  quiet-row button/styles: a waiting task opens its Work detail, a page opens the Needs you
 *  group, and a stopped goal keeps its Keep going / Drop the goal controls (work/sections/goals).
 *  No status dots, chevrons or dividers on any row. */
import { useState } from "react";
import { Button } from "@protolabsai/ui/primitives";

import { api } from "../lib/api";
import type { GoalState, InboxItem, Task } from "../lib/types";
import type { AnswerResult, DecisionRow } from "../lib/api/decisions";
import { DecisionCard } from "../decisions/DecisionCard";
import { navigate } from "../app/palette/nav";
import { openSourceChat } from "../work/openSourceChat";
import { relTime, type GroupableWorkItem } from "../work/tone";
import { chatStore } from "./chat-store";

/** The kinds this section draws rows for — the rail (ChatStatusRail) counts EXACTLY this
 *  set, so "Needs you · N" can never claim a row the panel doesn't show. A `waiting` task
 *  (ADR 0129 D8) opens its Work detail to answer in place, even without a source chat
 *  — so it counts here exactly as it does in Work's group. */
export const NEEDS_YOU_SECTION_KINDS: ReadonlySet<GroupableWorkItem["kind"]> =
  new Set(["decision", "delivery", "goal", "task"]);

/** A decision pending this long gets the muted stale hint — never a warning callout. */
export const STALE_DECISION_MS = 30 * 60_000;
export function decisionStale(createdAt: string, now: number = Date.now()): boolean {
  const asked = new Date(createdAt).getTime();
  return Number.isFinite(asked) && now - asked >= STALE_DECISION_MS;
}

/** The session id to open as a read-only Conversations thread (C6a's `#/conversations/<id>`
 *  route, §4.6), or null when there is no thread to open: this agent's own `chat-…` session
 *  (it opens as a chat, not here), a `job:…` background job, or an absent id. */
export function conversationThreadId(sessionId: string | null | undefined): string | null {
  return sessionId && !sessionId.startsWith("chat-") && !sessionId.startsWith("job:") ? sessionId : null;
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
  openWork: (itemId?: string) => void;
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
  const tasks = items.filter((item) => item.kind === "task");
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

  // §4.6 (C6b): an item from a non-chat, non-job session — a real A2A context — gets a quiet
  // ghost button that closes the panel and deep-links to its read-only Conversations thread
  // (C6a's #/conversations/<id> route). Items from this agent's own chat-… sessions get none.
  const conversationButton = (sessionId: string | null | undefined) => {
    const contextId = conversationThreadId(sessionId);
    return contextId ? (
      <Button
        size={size}
        variant="ghost"
        className="chat-needs-you__open-conversation"
        onClick={() => {
          onNavigate();
          navigate({ kind: "conversation", contextId });
        }}
      >
        Open conversation
      </Button>
    ) : null;
  };

  const name = count > 0 ? `Needs you, ${count}` : "Needs you";
  return (
    <section className="chat-status-details__section chat-needs-you" aria-label={name}>
      <h2 className="chat-status-details__heading" aria-label={name}>
        Needs you{count > 0 && <span aria-hidden="true"> · {count}</span>}
      </h2>
      {note && <p className="chat-status-details__problem" role="presentation">{note}</p>}
      {/* The one scroller: a long decision (many options, long explanations) scrolls here, so
          the heading above and "Open in Work" below stay in view and the panel never grows
          past the window. */}
      <div className="chat-needs-you__scroll">
      {rows.map((row, index) => {
        // The first pending decision stays the expanded DecisionCard; so does any the operator
        // has opened (expanded) or interacted with (held, §5.5). The rest are quiet rows.
        if (index === 0 || expanded.has(row.id) || held.has(row.id)) {
          return (
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
              {conversationButton(row.session_id)}
            </div>
          );
        }
        // A collapsed decision (D4): the question over a quiet "<agent> · asked <age>" line.
        // Selecting it expands this same row into a DecisionCard in place — it never navigates.
        const asked = relTime(row.created_at).text;
        const cue = [row.agent || "An agent", asked && `asked ${asked}`].filter(Boolean).join(" · ");
        return (
          <button
            key={row.id}
            type="button"
            className="chat-status-row"
            aria-expanded={false}
            title={row.question}
            aria-label={[row.question, cue].filter(Boolean).join(". ")}
            onClick={() => setExpanded((prev) => new Set(prev).add(row.id))}
          >
            <span className="chat-status-row__text">{row.question}</span>
            {cue ? <span className="chat-status-row__cue">{cue}</span> : null}
          </button>
        );
      })}
      {(tasks.length > 0 || deliveries.length > 0 || goals.length > 0) && (
        <ul className="chat-status-details__rows chat-status-group__rows">
          {tasks.map((item) => {
            // A `waiting` task (ADR 0129 Q1): its row closes the panel and opens the task's Work
            // detail (openWork("task:<id>")) to answer in place — no per-row action button.
            const age = relTime((item.raw as Task).updated_at ?? "").text;
            const cue = ["Waiting on you", age].filter(Boolean).join(" · ");
            // A waiting task raised from a non-chat A2A context trails the row with an "Open
            // conversation" link to its thread; the row itself still opens the Work detail.
            const convButton = conversationButton((item.raw as Task).session_id);
            return (
              <li key={`task:${item.id}`} className={convButton ? "chat-needs-you__task" : undefined}>
                <button
                  type="button"
                  className="chat-status-row"
                  title={item.title}
                  aria-label={[item.title, cue, "Open in Work"].filter(Boolean).join(". ")}
                  onClick={() => {
                    onNavigate();
                    openWork(`task:${item.id}`);
                  }}
                >
                  <span className="chat-status-row__text">{item.title}</span>
                  <span className="chat-status-row__cue">{cue}</span>
                </button>
                {convButton}
              </li>
            );
          })}
          {deliveries.map((item) => {
            // An undelivered human-needed page: its row opens the Needs you group in Work.
            const cue = [(item.raw as InboxItem).source, "couldn't be delivered"].filter(Boolean).join(" ");
            return (
              <li key={`delivery:${item.id}`}>
                <button
                  type="button"
                  className="chat-status-row"
                  title={item.title}
                  aria-label={[item.title, cue, "Open in Work"].filter(Boolean).join(". ")}
                  onClick={() => {
                    onNavigate();
                    openWork();
                  }}
                >
                  <span className="chat-status-row__text">{item.title}</span>
                  <span className="chat-status-row__cue">{cue}</span>
                </button>
              </li>
            );
          })}
          {goals.map((item) => {
            // A stopped goal keeps its own Keep going / Drop the goal controls (with the inline
            // confirm), so it is a quiet-framed row, not a navigable button.
            const goal = item.raw as GoalState;
            return (
              <li key={`goal:${item.id}`} className="chat-needs-you__goal">
                <span className="chat-status-row__text">{item.title}</span>
                <span className="chat-status-row__cue">Goal stopped</span>
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
      </div>
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
