import { type CSSProperties, type ReactNode, useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Avatar, Badge, Button, Callout, TextLink } from "@protolabsai/ui/primitives";
import { Alert, StatusDot } from "@protolabsai/ui/data";
import { Tooltip } from "@protolabsai/ui/overlays";

import type { AnswerResult, DecisionRow } from "../lib/api/decisions";
import { formatGivenAt, localStampTitle } from "../lib/format";

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
  /** Send the chosen option. Resolves to the answer result, including a 409 answered-elsewhere. */
  onAnswer: (optionId: string) => Promise<AnswerResult>;
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
};

type Phase =
  | { kind: "idle" }
  | { kind: "sending"; optionId: string }
  | { kind: "sent"; label: string }
  | { kind: "answeredElsewhere"; record: DecisionRow | null }
  | { kind: "error"; optionId: string };

const TERMINAL: ReadonlySet<DecisionRow["state"]> = new Set(["answered", "expired", "cancelled", "withdrawn"]);

export function DecisionCard({
  decision,
  onAnswer,
  onHold,
  onResolved,
  onOpenChat,
  onReplyInChat,
  chatTitle,
  agentOnline = true,
  agentHoldsOffline = true,
  onStartAgent,
}: DecisionCardProps) {
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [expanded, setExpanded] = useState(false);
  const [clipped, setClipped] = useState(false);
  const question = useRef<HTMLParagraphElement>(null);
  const result = useRef<HTMLDivElement>(null);
  const labelId = useId();
  const groupId = useId();

  const agent = decision.agent || "An agent";
  const offlineBlocks = !agentOnline && !agentHoldsOffline;
  const optionsDisabled = phase.kind === "sending" || offlineBlocks;

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
    if (phase.kind === "sent" || phase.kind === "error" || phase.kind === "answeredElsewhere") {
      result.current?.focus();
    }
  }, [phase.kind]);

  // The 4-second "Sent" hold, then the card leaves (§5.5).
  useEffect(() => {
    if (phase.kind !== "sent") return;
    const timer = setTimeout(() => onResolved?.(decision.id), 4000);
    return () => clearTimeout(timer);
  }, [phase, decision.id, onResolved]);

  async function choose(optionId: string) {
    onHold?.(decision);
    setPhase({ kind: "sending", optionId });
    try {
      const outcome = await onAnswer(optionId);
      if (outcome.answered_elsewhere) {
        setPhase({ kind: "answeredElsewhere", record: outcome.decision });
      } else {
        setPhase({ kind: "sent", label: optionLabel(optionId) });
      }
    } catch {
      // Nothing changed server-side; keep the options enabled so Josh can retry or pick another.
      setPhase({ kind: "error", optionId });
    }
  }

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

  function closedCallout() {
    if (decision.state === "expired") {
      const when = decision.expires_at ? formatGivenAt(decision.expires_at) : "the deadline";
      return (
        <Callout tone="neutral">
          {`No answer by ${when}, so ${agent} went ahead and ${decision.default_action_text}.`}
        </Callout>
      );
    }
    if (decision.state === "answered") return answeredElsewhereCallout(decision);
    // withdrawn / cancelled — "no longer needed"
    const reason = decision.withdraw_reason?.trim();
    return (
      <Callout tone="neutral">
        {`${agent} doesn't need this any more.${reason ? ` ${reason}` : ""}`}
      </Callout>
    );
  }

  let body: ReactNode;
  if (phase.kind === "answeredElsewhere") {
    body = (
      <div ref={result} tabIndex={-1} aria-live="polite">
        {phase.record ? answeredElsewhereCallout(phase.record) : answeredElsewhereCallout(decision)}
      </div>
    );
  } else if (phase.kind === "sent") {
    const sent = agentOnline
      ? `Sent: "${phase.label}". ${agent} is picking it back up.`
      : `Saved: "${phase.label}". ${agent} gets it when it starts again.`;
    body = (
      <div ref={result} tabIndex={-1} aria-live="polite">
        <StatusDot status="success" label={sent} />
      </div>
    );
  } else if (phase.kind === "idle" && TERMINAL.has(decision.state)) {
    body = (
      <div aria-live="polite">{closedCallout()}</div>
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
                <Button variant="ghost" size="sm" onClick={() => void choose(phase.optionId)}>
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
                  size="md"
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
          <Button variant="ghost" size="md" onClick={() => onReplyInChat?.(decision)}>
            Reply in chat instead
          </Button>
        </div>
        {offlineBlocks ? (
          <p style={muted}>
            {`${agent} is offline. Start it to answer.`}{" "}
            <Button variant="ghost" size="sm" onClick={() => onStartAgent?.(decision)}>
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
        <Button variant="ghost" size="sm" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
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
        <TextLink
          href="#"
          onClick={(e) => {
            e.preventDefault();
            onOpenChat?.(decision);
          }}
        >
          Open chat: {chatTitle || agent}
        </TextLink>
      </div>
    </article>
  );
}
