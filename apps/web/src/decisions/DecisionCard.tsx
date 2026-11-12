import { type CSSProperties, type ReactNode, useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Avatar, Badge, Button, Callout, TextLink } from "@protolabsai/ui/primitives";
import { Alert, StatusDot } from "@protolabsai/ui/data";
import { Tooltip } from "@protolabsai/ui/overlays";

import type { AnswerResult, DecisionRow } from "../lib/api/decisions";
import { formatGivenAt, localStampTitle } from "../lib/format";
import { recheckDecision } from "./useDecisions";

// Tokens only (§5.10): question = --pl-color-fg; meta, option explanations and the "if nobody
// answers" line = --pl-color-fg-muted (7.24:1 dark / 7.73:1 light on --pl-color-bg-raised).
// Never --pl-color-fg-subtle for card text — it fails at 3.42:1 on the light surface.
const card: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--pl-space-3)" };
const metaRow: CSSProperties = {
  display: "flex",
  alignItems: "center",
  flexWrap: "wrap",
  gap: "var(--pl-space-2)",
  color: "var(--pl-color-fg-muted)",
};
const agentName: CSSProperties = { color: "var(--pl-color-fg)" };
const questionBase: CSSProperties = {
  color: "var(--pl-color-fg)",
  whiteSpace: "pre-wrap",
  margin: 0,
};
const questionClamp: CSSProperties = {
  ...questionBase,
  display: "-webkit-box",
  WebkitBoxOrient: "vertical",
  WebkitLineClamp: 6,
  overflow: "hidden",
};
const optionGroup: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--pl-space-2)" };
const optionWrap: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--pl-space-1)" };
const optionBtn: CSSProperties = { width: "100%", justifyContent: "flex-start", gap: "var(--pl-space-2)" };
const muted: CSSProperties = { color: "var(--pl-color-fg-muted)", margin: 0 };
const footerRow: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--pl-space-2)" };

export type DecisionCardProps = {
  decision: DecisionRow;
  actionSize?: "sm" | "md";
  /** Send the chosen option. Resolves to the answer result, including a 409 answered-elsewhere. */
  onAnswer: (optionId: string) => Promise<AnswerResult>;
  /** Re-read this decision from the server (bypassing any cache) — how the card finds out
   *  whether an answer landed when the send's outcome is unknown: the request failed in
   *  transit, or the server said it is on its way. Resolves null when the decision is gone;
   *  rejects when the server can't be reached. Defaults to the shared decisions store. */
  onCheck?: (id: string) => Promise<DecisionRow | null>;
  /** The card entered an interaction (sending); the panel retains it through its "Sent" hold so
   *  it never vanishes under the pointer (§5.5). */
  onHold?: (decision: DecisionRow) => void;
  /** The card reached a settled, done state (the 4-second "Sent" hold elapsed) — drop it. */
  onResolved?: (id: string) => void;
  /** Open the source chat (switch agent + chat, then close the panel). Mount wiring, optional here. */
  onOpenChat?: (decision: DecisionRow) => void;
  /** Open the chat with the composer focused, for a free-text reply (§4.3). */
  onReplyInChat?: (decision: DecisionRow) => void;
  /** Title of the source chat for "Open chat: {title}"; falls back to the agent name. */
  chatTitle?: string;
  /** Agent presence. Default online; offline adds a neutral StatusDot + line (§5.7). */
  agentOnline?: boolean;
  /** Whether the backend holds an answer for a stopped agent (Q3). When false AND offline, the
   *  options are disabled and a Start action shows instead. Default true. */
  agentHoldsOffline?: boolean;
  onStartAgent?: (decision: DecisionRow) => void;
  /** How the card presents (§5.5–5.6). "panel" (default) keeps the Decisions panel's behaviour:
   *  a successful answer holds "Sent" for 4s then calls onResolved, and a settled or 409 record
   *  shows a Callout. "transcript" is the source-chat copy: a successful answer freezes on "Sent"
   *  (onResolved is never called, the card never leaves the transcript), and a settled or 409
   *  record freezes as a single plain line instead of a callout. A pending decision renders
   *  identically in both. */
  presentation?: "panel" | "transcript";
};

type Phase =
  | { kind: "idle" }
  | { kind: "sending"; optionId: string }
  | { kind: "sent"; label: string }
  | { kind: "answeredElsewhere"; record: DecisionRow | null }
  | { kind: "error"; optionId: string }
  // The send's outcome is unknown (#5099): the request failed in transit, or the server said
  // the answer is on its way (`delivering`). The card re-reads the decision until it knows —
  // it never offers the options again while an answer may still land. `note` picks the line.
  | { kind: "checking"; optionId: string; note: "checking" | "onItsWay" | "unreachable"; attempt: number };

/** Re-check delays while an answer's outcome is unknown: quick at first, then every 5s. */
const CHECK_DELAYS_MS = [1000, 2000, 3000, 5000];

const TERMINAL: ReadonlySet<DecisionRow["state"]> = new Set(["answered", "expired", "cancelled", "withdrawn"]);

export function DecisionCard({
  decision,
  onAnswer,
  onCheck = recheckDecision,
  onHold,
  onResolved,
  onOpenChat,
  onReplyInChat,
  chatTitle,
  agentOnline = true,
  agentHoldsOffline = true,
  onStartAgent,
  presentation = "panel",
  actionSize,
}: DecisionCardProps) {
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [expanded, setExpanded] = useState(false);
  const [clipped, setClipped] = useState(false);
  const question = useRef<HTMLParagraphElement>(null);
  const result = useRef<HTMLDivElement>(null);
  const labelId = useId();
  const groupId = useId();

  const agent = decision.agent || "An agent";
  const isTranscript = presentation === "transcript";
  const offlineBlocks = !agentOnline && !agentHoldsOffline;
  const optionsDisabled = phase.kind === "sending" || phase.kind === "checking" || offlineBlocks;

  const optionLabel = (optionId: string) =>
    decision.options.find((o) => o.id === optionId)?.label ?? optionId;

  useLayoutEffect(() => {
    const node = question.current;
    if (!node || expanded) return;
    const measure = () => setClipped(node.scrollHeight > node.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [decision.question, expanded]);

  // Move focus to the result/alert when it appears (§5.5, §5.8).
  useLayoutEffect(() => {
    if (phase.kind === "sent" || phase.kind === "error" || phase.kind === "answeredElsewhere" || phase.kind === "checking") {
      result.current?.focus();
    }
  }, [phase.kind]);

  // The 4-second "Sent" hold, then the card leaves (§5.5). Only in the panel — in a chat
  // transcript the answered card freezes on "Sent" and never leaves (§5.5.4), so no hold.
  useEffect(() => {
    if (isTranscript || phase.kind !== "sent") return;
    const timer = setTimeout(() => onResolved?.(decision.id), 4000);
    return () => clearTimeout(timer);
  }, [isTranscript, phase, decision.id, onResolved]);

  // What a (re-)read record says about an answer we sent for `optionId`. "unknown" keeps the
  // card checking: still pending while an answer is on its way.
  function settle(record: DecisionRow | null, optionId: string): Phase | "unknown" {
    if (record === null) return { kind: "answeredElsewhere", record: null };
    if (record.state === "answered" && record.answer === optionId) {
      return { kind: "sent", label: optionLabel(optionId) };
    }
    if (TERMINAL.has(record.state)) return { kind: "answeredElsewhere", record };
    // Pending with nothing on its way: the server confirms the answer never reached the agent.
    return record.delivering ? "unknown" : { kind: "error", optionId };
  }

  async function choose(optionId: string) {
    onHold?.(decision);
    setPhase({ kind: "sending", optionId });
    try {
      const outcome = await onAnswer(optionId);
      if (outcome.answered_elsewhere) {
        setPhase({ kind: "answeredElsewhere", record: outcome.decision });
      } else if (outcome.delivering) {
        // 202: accepted, but the agent hasn't taken it yet (its session is busy). Not sent,
        // not failed — watch for it to land (#5099).
        setPhase({ kind: "checking", optionId, note: "onItsWay", attempt: 0 });
      } else if (!outcome.resumed) {
        // 200 but the parked turn was NOT resumed: the server confirmed delivery failed and
        // the decision is still pending (operator_api/decision_routes.py). Never claim
        // success; keep the options enabled to retry (§4.4).
        setPhase({ kind: "error", optionId });
      } else {
        setPhase({ kind: "sent", label: optionLabel(optionId) });
      }
    } catch {
      // The request failed in transit (or the server errored): the answer may or may not
      // have reached the agent. Find out before saying anything, and offer no retry until the
      // server confirms the decision is still waiting (#5099).
      setPhase({ kind: "checking", optionId, note: "checking", attempt: 0 });
    }
  }

  // While an answer's outcome is unknown, re-read the decision on a backoff until it says.
  const checking = phase.kind === "checking" ? phase : null;
  useEffect(() => {
    if (!checking) return;
    let live = true;
    const delay = checking.attempt === 0 && checking.note === "checking"
      ? 0 : CHECK_DELAYS_MS[Math.min(checking.attempt, CHECK_DELAYS_MS.length - 1)];
    const timer = setTimeout(() => {
      onCheck(decision.id).then(
        (record) => {
          if (!live) return;
          const next = settle(record, checking.optionId);
          setPhase(next === "unknown"
            ? { kind: "checking", optionId: checking.optionId, note: "onItsWay", attempt: checking.attempt + 1 }
            : next);
        },
        () => {
          if (live) setPhase({ kind: "checking", optionId: checking.optionId, note: "unreachable", attempt: checking.attempt + 1 });
        },
      );
    }, delay);
    return () => {
      live = false;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-armed per attempt only
  }, [checking?.attempt, checking?.note, checking?.optionId, decision.id]);

  // A live record update (decision.changed) can settle it before the next check.
  useEffect(() => {
    if (!checking || !TERMINAL.has(decision.state)) return;
    const next = settle(decision, checking.optionId);
    if (next !== "unknown") setPhase(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reacts to the record only
  }, [decision.state, decision.answer]);

  const firstWords = decision.question.replace(/\s+/g, " ").trim().slice(0, 80);

  // ── result / closed states (replace the options) ──────────────────────────────────────────
  function answeredElsewhereCallout(record: DecisionRow) {
    const chosen = record.answer_label ?? record.answer ?? "an option";
    const when = record.answered_at ? ` at ${formatGivenAt(record.answered_at)}` : "";
    return (
      <Callout tone="info">
        {`Already answered: you chose "${chosen}"${when}. Nothing else changed.`}
      </Callout>
    );
  }

  function closedCallout(record: DecisionRow) {
    if (record.state === "expired") {
      const when = record.expires_at ? formatGivenAt(record.expires_at) : "the deadline";
      return (
        <Callout tone="neutral">
          {`No answer by ${when}, so ${agent} went ahead and ${record.default_action_text}.`}
        </Callout>
      );
    }
    if (record.state === "answered") return answeredElsewhereCallout(record);
    // withdrawn / cancelled — "no longer needed"
    const reason = record.withdraw_reason?.trim();
    return (
      <Callout tone="neutral">
        {`${agent} doesn't need this any more.${reason ? ` ${reason}` : ""}`}
      </Callout>
    );
  }

  // The frozen transcript sentence for a settled record (§5.5–5.6): one plain muted line, never
  // a callout. Answered reads "You chose …"; expired and withdrawn keep the panel's closed text.
  function transcriptLine(record: DecisionRow) {
    if (record.state === "answered") {
      const chosen = record.answer_label ?? record.answer ?? "an option";
      return `You chose "${chosen}" · ${formatGivenAt(record.answered_at)}`;
    }
    if (record.state === "expired") {
      const when = record.expires_at ? formatGivenAt(record.expires_at) : "the deadline";
      return `No answer by ${when}, so ${agent} went ahead and ${record.default_action_text}.`;
    }
    // withdrawn / cancelled — "no longer needed"
    const reason = record.withdraw_reason?.trim();
    return `${agent} doesn't need this any more.${reason ? ` ${reason}` : ""}`;
  }

  let body: ReactNode;
  if (isTranscript && TERMINAL.has(decision.state) && !(phase.kind === "sent" && decision.state === "answered")) {
    // Settled record in a chat transcript: one frozen line, no options, no callout (§5.5.4, §5.6).
    // Keep the local successful send visible; historical and externally closed cards use the record.
    body = (
      <p style={muted} aria-live="polite">
        {transcriptLine(decision)}
      </p>
    );
  } else if (phase.kind === "answeredElsewhere") {
    // The API uses answered_elsewhere for every 409, including expiry and withdrawal.
    // Prefer its terminal record while retaining the legacy missing-record fallback.
    const record = phase.record ?? decision;
    body = isTranscript ? (
      // A 409 under us in a transcript freezes on the returned record's frozen line, not the callout.
      <p ref={result} tabIndex={-1} style={muted} aria-live="polite">
        {transcriptLine(record)}
      </p>
    ) : (
      <div ref={result} tabIndex={-1} aria-live="polite">
        {TERMINAL.has(record.state)
          ? closedCallout(record)
          : answeredElsewhereCallout(record)}
      </div>
    );
  } else if (phase.kind === "checking") {
    // Information, not a problem: neutral, no options, no retry (#5099).
    const label = optionLabel(phase.optionId);
    const line = phase.note === "onItsWay"
      ? `Sent: "${label}". Waiting for ${agent} to pick it up…`
      : phase.note === "unreachable"
        ? `Can't confirm yet whether ${agent} got "${label}". Still checking…`
        : `Checking whether ${agent} got "${label}"…`;
    body = (
      <div ref={result} tabIndex={-1} aria-live="polite">
        <StatusDot status="neutral" pulse label={line} />
      </div>
    );
  } else if (phase.kind === "sent") {
    const sent = agentOnline
      ? `Sent: "${phase.label}". Continuing…`
      : `Saved: "${phase.label}". ${agent} gets it when it starts again.`;
    body = (
      <div ref={result} tabIndex={-1} aria-live="polite">
        <StatusDot status="success" label={sent} />
      </div>
    );
  } else if ((phase.kind === "idle" || phase.kind === "error") && TERMINAL.has(decision.state)) {
    // A failed send leaves the error phase, but a record that has since settled elsewhere is
    // final: show it as closed rather than offering "Try again" on a decision nobody can answer.
    body = (
      <div aria-live="polite">{closedCallout(decision)}</div>
    );
  } else {
    // pending (idle / sending / error): show the options
    body = (
      <>
        {phase.kind === "error" && (
          <div ref={result} tabIndex={-1} aria-live="polite">
            <Alert
              status="warning"
              action={
                <Button variant="ghost" size={actionSize ?? "sm"} onClick={() => void choose(phase.optionId)}>
                  Try again
                </Button>
              }
            >
              {`Couldn't send your answer. Nothing changed, and ${agent} is still waiting.`}
            </Alert>
          </div>
        )}
        <div role="group" aria-label="Answer" style={optionGroup}>
          {decision.options.map((option) => {
            const descId = option.description ? `${groupId}-${option.id}-desc` : undefined;
            const sendingThis = phase.kind === "sending" && phase.optionId === option.id;
            return (
              <div key={option.id} style={optionWrap}>
                <Button
                  variant={option.recommended ? "primary" : "default"}
                  size={actionSize ?? "md"}
                  style={optionBtn}
                  disabled={optionsDisabled && !sendingThis}
                  loading={sendingThis}
                  aria-describedby={descId}
                  onClick={() => void choose(option.id)}
                >
                  <span>{option.label}</span>
                  {option.recommended && <Badge>suggested</Badge>}
                </Button>
                {option.description && (
                  <p id={descId} style={muted}>
                    {option.description}
                  </p>
                )}
              </div>
            );
          })}
          <Button variant="ghost" size={actionSize ?? "md"} onClick={() => onReplyInChat?.(decision)}>
            Reply in chat instead
          </Button>
        </div>
        {offlineBlocks ? (
          <p style={muted}>
            {`${agent} is offline. Start it to answer.`}{" "}
            <Button variant="ghost" size={actionSize ?? "sm"} onClick={() => onStartAgent?.(decision)}>
              Start
            </Button>
          </p>
        ) : !agentOnline ? (
          <p style={muted}>{`${agent} is offline. It gets your answer when it starts again.`}</p>
        ) : null}
      </>
    );
  }

  return (
    <article aria-label={`${agent}: ${firstWords}`} style={card}>
      <div style={metaRow}>
        <Avatar name={agent} size={24} />
        <strong style={agentName}>{agent}</strong>
        {!agentOnline && <StatusDot status="neutral" label="offline" />}
        <span aria-hidden>·</span>
        <Tooltip label={localStampTitle(decision.created_at)}>
          <span>asked {formatGivenAt(decision.created_at)}</span>
        </Tooltip>
      </div>

      <p
        ref={question}
        id={labelId}
        style={expanded ? questionBase : questionClamp}
      >
        {decision.question}
      </p>
      {(clipped || expanded) && (
        <Button variant="ghost" size={actionSize ?? "sm"} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          {expanded ? "Show less" : "Show full question"}
        </Button>
      )}

      {body}

      <div style={footerRow}>
        {decision.expires_at && phase.kind === "idle" && !TERMINAL.has(decision.state) && (
          <p style={muted}>
            {`If nobody answers by ${formatGivenAt(decision.expires_at)}, ${agent} will ${decision.default_action_text}.`}
          </p>
        )}
        {/* In the source chat's transcript the card is already in that chat: no link to it. */}
        {!isTranscript && (
          <TextLink
            href="#"
            onClick={(e) => {
              e.preventDefault();
              onOpenChat?.(decision);
            }}
          >
            Open chat: {chatTitle || agent}
          </TextLink>
        )}
      </div>
    </article>
  );
}
