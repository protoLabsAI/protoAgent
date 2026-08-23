import { Button } from "@protolabsai/ui/primitives";
import { Message, MessageAction, MessageActions } from "@protolabsai/ui/ai";
import { Tooltip } from "@protolabsai/ui/overlays";
import { Spinner } from "@protolabsai/ui/data";
import { ArrowDownToLine, CalendarClock, Check, Clock, Coins, Copy, FileText, GitBranch, Gauge, History, RotateCcw, X } from "lucide-react";
import { useState } from "react";

import { useQuery } from "@tanstack/react-query";

import { openDocument } from "../docviewer";
import { slashCommandName } from "../ext/slashRegistry";
import { loadBackgroundReport } from "../lib/api";
import { runtimeStatusQuery } from "../lib/queries";
import {
  costAriaLabel,
  costPrefix,
  costTipLabel,
  costTipSub,
  isSubscriptionProvider,
  usageTipNote,
} from "./costLabel";
import { useUI } from "../state/uiStore";
import type { ChatMessage, ChatPart, ContextWindow, TurnUsage } from "../lib/types";
import { ChatComponent } from "./ChatComponent";
import { Markdown } from "./LazyMarkdown";
import { openPromptViewer } from "./PromptViewer";
import { ReasoningCard } from "./ReasoningCard";
import { ToolCalls } from "./ToolCalls";
import { WorkBlock } from "./WorkBlock";
import { foldPlan, toolsForGroup } from "./parts";

// Optional per-message action row (copy / fork / regenerate). Omit it (e.g. the ⌘K palette
// chat) and no actions render. Each callback is independently optional.
export type ChatMessageActions = {
  copiedId?: string | null;
  // Session the message belongs to — lets View prompt (#2843) fetch the history
  // breakdown alongside the captured prompt.
  sessionId?: string;
  onCopy?: (m: ChatMessage) => void;
  onFork?: (m: ChatMessage) => void;
  onRewind?: (m: ChatMessage) => void;
  onRegenerate?: (id: string) => void;
  // Remove a local-only errored turn from the transcript (#1695) — the error bubble is
  // display-only (not backend history), so a reload clears it; this offers a dismiss in place.
  onDismiss?: (id: string) => void;
  lastAssistantId?: string;
  regenDisabled?: boolean;
  // The conversational tail (chat/parts.rewindableTailId): "Rewind to here" hides on
  // it — discarding "everything below" the tail is a no-op behind a destructive
  // confirm, which reads as either scary or broken (Josh, 2026-08-10).
  rewindTailId?: string;
  // Incognito sessions retain no prompt snapshots BY DESIGN — offering "View prompt"
  // there 404s and reads as data loss instead of privacy working (#2484).
  incognito?: boolean;
};

// The single chat message renderer (ADR 0035) — shared by the main chat (ChatSurface) and the
// ⌘K palette chat (PaletteChat) so they never drift. Renders one user/assistant/system message:
// live ordered `parts` (text↔tool interleave, WorkBlock fold) or the history-loaded grouped
// fallback, plus the streaming loader, inline components, the background-report chip, and the
// optional action row. Streaming state is read from `message.status`.
export function ChatMessageView({
  message,
  onCancelDelegation,
  actions,
}: {
  message: ChatMessage;
  onCancelDelegation?: (id: string) => void;
  actions?: ChatMessageActions;
}) {
  const streaming = message.status === "streaming";
  // Per-turn token/cost footer is an opt-out display pref (Settings ▸ Chat, #1372).
  const showChatUsage = useUI((s) => s.showChatUsage);
  // An issued slash command (/goal …) renders as a distinct user bubble (subtle tint /
  // monospace / "/" badge) so it reads as a command in history, to operator and agent (#1529).
  const userSlashCmd = message.role === "user" ? slashCommandName(message.content) : null;
  // Literal user text — a monospace "/command" chip when it's a slash command, else plain
  // whitespace-preserving text. Shared by the ordered-parts and history render paths.
  const renderUserText = (text: string, key?: string) =>
    slashCommandName(text) ? (
      <span className="chat-user-text chat-slash-cmd" key={key}>
        <span className="chat-slash-badge" aria-hidden>
          /
        </span>
        <span className="chat-slash-body">{text.replace(/^\s*\//, "")}</span>
      </span>
    ) : (
      <span className="chat-user-text" key={key}>
        {text}
      </span>
    );
  // Background-agent report (ADR 0050 → ADR 0070 D4 → #2923): a finished job's report renders
  // as a dedicated CHIP — none of the streaming/parts machinery below applies (BackgroundWatch
  // injects it display-only: role "system", plain content, never streams).
  if (message.report) {
    return <BackgroundReportCard message={message} report={message.report} />;
  }
  // Scheduled-task result (#2990): a fire's outcome delivered back to the chat that set
  // the schedule up. First fire → a full ScheduledReportCard (calendar/clock icon, distinct
  // from the background FileText chip); recurring re-fires → a compact one-line ScheduledChip
  // so an hourly check can't fill the thread. Injected display-only, like the report card.
  if (message.scheduled) {
    return message.scheduled.collapse ? (
      <ScheduledChip message={message} scheduled={message.scheduled} />
    ) : (
      <ScheduledReportCard message={message} scheduled={message.scheduled} />
    );
  }
  return (
    <Message
      role={message.role}
      streaming={streaming}
      className={
        // Any system message here is a local note → compact .chat-note card; the tone
        // modifier is appended only when set, so neutral notes still get the styling.
        // (Report messages returned above.)
        message.role === "system"
          ? `chat-note${message.noteTone ? ` chat-note--${message.noteTone}` : ""}`
          : // An issued slash command → distinct .chat-slash-msg user bubble (#1529).
            userSlashCmd
            ? "chat-slash-msg"
            : undefined
      }
    >
      {message.reasoning && !(message.parts && message.parts.length) ? (
        // History-loaded turns have no ordered parts — fall back to the flat collapsed
        // reasoning card. Live turns render reasoning inline via parts.
        <ReasoningCard text={message.reasoning} streaming={streaming && !message.content} />
      ) : null}
      {message.parts && message.parts.length ? (
        (() => {
          // Fold the intermediate reason→tool timeline behind ONE WorkBlock so the answer
          // leads — but ONLY when the turn interleaves reasoning WITH tools (the forever-stack
          // case). Tool-only / reasoning-only turns keep their inline cards; a plain turn is
          // just the answer. The "answer" is the trailing run of text parts; everything before
          // it is the work. User messages carry only text.
          const parts = message.parts!;
          // The "answer" is the trailing run of text AND component parts — a component is emitted
          // before its summary text, so it belongs with the answer (above the text), not folded
          // into the work timeline (#1323). Everything before is the work. While STREAMING a folded
          // (reason+tool) turn, foldPlan keeps an ambiguous trailing run as work so it can't flash
          // into the main chat and then jump back into the WorkBlock when the next tool arrives.
          const { fold, workParts, answerParts } = foldPlan(parts, streaming);
          const renderText = (part: ChatPart, key: string) =>
            part.kind !== "text" || !part.text.trim() ? null : message.role === "user" ? (
              renderUserText(part.text, key)
            ) : (
              <Markdown key={key}>{part.text}</Markdown>
            );
          const renderInline = (part: ChatPart, i: number) =>
            part.kind === "tools" ? (
              <ToolCalls key={i} calls={toolsForGroup(part.ids, message.toolCalls)} streaming={streaming} onCancelDelegation={onCancelDelegation} />
            ) : part.kind === "reasoning" ? (
              part.text.trim() ? (
                <ReasoningCard key={i} text={part.text} streaming={streaming && i === workParts.length - 1} />
              ) : null
            ) : part.kind === "component" ? (
              <ChatComponent key={i} spec={part.spec} />
            ) : (
              renderText(part, `w${i}`)
            );
          // An answer part is either streamed text or an inline component (rendered in order).
          const renderAnswerPart = (part: ChatPart, i: number) =>
            part.kind === "component" ? <ChatComponent key={`ac${i}`} spec={part.spec} /> : renderText(part, `a${i}`);
          return (
            <>
              {fold ? (
                <WorkBlock parts={workParts} toolCalls={message.toolCalls} streaming={streaming} />
              ) : (
                workParts.map(renderInline)
              )}
              {answerParts.map(renderAnswerPart)}
            </>
          );
        })()
      ) : (
        // History-loaded messages have no ordered parts — keep the grouped layout
        // (tool cards above the text; order isn't recoverable from storage).
        <>
          {message.toolCalls && message.toolCalls.length > 0 ? (
            <ToolCalls calls={message.toolCalls} onCancelDelegation={onCancelDelegation} />
          ) : null}
          {message.content ? (
            message.role === "user" ? (
              renderUserText(message.content)
            ) : (
              <Markdown>{message.content}</Markdown>
            )
          ) : null}
        </>
      )}
      {streaming &&
      !(message.parts && message.parts.length) &&
      !message.content &&
      !(message.toolCalls && message.toolCalls.length) &&
      !(message.components && message.components.length) &&
      !message.reasoning ? (
        <Spinner size={15} />
      ) : null}
      {/* History fallback: a message persisted before component-parts existed renders its
          components here (after the answer). Live turns render them inline via ordered parts
          above — so skip this when any component part is present, to avoid double-rendering. */}
      {message.components && message.components.length > 0 && !message.parts?.some((p) => p.kind === "component")
        ? message.components.map((spec, i) => <ChatComponent key={i} spec={spec} />)
        : null}
      {/* Mid-turn activity cue (#2967): once ANY content has landed, the empty-message spinner
          above never renders again — so a generation pause (between tool calls, during a long
          tool run, while large tool-call args stream) reads as a dead stall. A smaller, subtle
          spinner sits at the BOTTOM of the streaming message: DOM order pushes it below each
          new part as it arrives, and it unmounts when `streaming` flips false. Reasoning-only
          turns are deliberately excluded — their live ReasoningCard already animates. */}
      {streaming && (message.parts?.length || message.content || message.toolCalls?.length) ? (
        <div className="chat-streaming-indicator">
          <Spinner size={12} />
        </div>
      ) : null}
      {showChatUsage && message.role === "assistant" && !streaming && (message.usage || message.contextWindow) ? (
        <UsageFooter usage={message.usage} context={message.contextWindow} />
      ) : null}
      {actions && message.role === "assistant" && !streaming && message.content ? (
        <MessageActions>
          {actions.onCopy ? (
            <MessageAction
              label={actions.copiedId === message.id ? "Copied" : "Copy"}
              icon={actions.copiedId === message.id ? <Check size={14} /> : <Copy size={14} />}
              onClick={() => actions.onCopy!(message)}
            />
          ) : null}
          {actions.onFork ? (
            <MessageAction label="Fork from here" icon={<GitBranch size={14} />} onClick={() => actions.onFork!(message)} />
          ) : null}
          {actions.onRewind && message.id !== actions.rewindTailId ? (
            <MessageAction label="Rewind to here" icon={<History size={14} />} onClick={() => actions.onRewind!(message)} />
          ) : null}
          {message.taskId && !actions.incognito ? (
            <MessageAction
              label="View prompt"
              icon={<FileText size={14} />}
              onClick={() => openPromptViewer(message.taskId!, actions.sessionId)}
            />
          ) : null}
          {actions.onRegenerate && message.id === actions.lastAssistantId ? (
            <MessageAction
              label="Regenerate"
              icon={<RotateCcw size={14} />}
              disabled={actions.regenDisabled}
              onClick={() => actions.onRegenerate!(message.id!)}
            />
          ) : null}
          {actions.onDismiss && message.status === "error" ? (
            <MessageAction label="Dismiss" icon={<X size={14} />} onClick={() => actions.onDismiss!(message.id!)} />
          ) : null}
        </MessageActions>
      ) : null}
    </Message>
  );
}

// Dismissed report chips (#2923): jobIds the operator ✕'d out of the transcript.
// localStorage (not sessionStorage) so a reload doesn't resurrect them — the report itself
// stays retrievable in the Background agents panel (GET /api/background); dismissal only
// affects chat rendering. Capped like the panel's seen-set (#2692).
const DISMISSED_KEY = "protoagent.chat.dismissedReports";

function dismissedSet(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(DISMISSED_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function useDismissedReports() {
  const [dismissed, setDismissed] = useState<Set<string>>(dismissedSet);
  const dismiss = (jobId: string) => {
    // Read-merge-write so a dismiss in another tab sharing this key isn't clobbered.
    const s = dismissedSet();
    s.add(jobId);
    try {
      localStorage.setItem(DISMISSED_KEY, JSON.stringify([...s].slice(-300)));
    } catch {
      /* storage unavailable — the chip still hides for this page's lifetime */
    }
    setDismissed(s);
  };
  return { dismissed, dismiss };
}

// The background-report CHIP (#2923; supersedes the ADR 0070 D4 teaser card). A finished
// job's report notification is ONE compact row — icon + title + "Open" + dismiss ✕ — the
// document viewer is the whole reading experience, so there is no excerpt/expand state in
// between. The full report is fetched BY ID (GET /api/background/{id});
// loadBackgroundReport keeps a legacy list-and-filter fallback for pre-ADR-0070 servers.
// Dismissing hides the chip from THIS transcript only (persisted per jobId); the
// Background agents panel remains the durable place the report lives.
function BackgroundReportCard({
  message,
  report,
}: {
  message: ChatMessage;
  report: NonNullable<ChatMessage["report"]>;
}) {
  const { dismissed, dismiss } = useDismissedReports();
  if (dismissed.has(report.jobId)) return null;
  const open = () =>
    openDocument({
      title: report.title,
      subtitle: "Background agent report",
      load: () => loadBackgroundReport(report.jobId),
    });
  return (
    <Message role={message.role} className="chat-report">
      <div className="chat-report-chip">
        <FileText size={14} aria-hidden />
        <span className="chat-report-title" title={report.title}>
          {report.title}
        </span>
        <Button className="chat-report-open" size="sm" variant="ghost" onClick={open}>
          Open
        </Button>
        <button
          type="button"
          className="chat-report-dismiss"
          onClick={() => dismiss(report.jobId)}
          title="Dismiss — the report stays in the Background agents panel"
          aria-label="Dismiss report"
        >
          <X size={14} />
        </button>
      </div>
    </Message>
  );
}

// Dismissed scheduled fires (#2990): keyed per FIRE (`jobId:firedAt`), not per job — a
// recurring schedule delivers many chips and dismissing one must not hide the rest.
// localStorage so a reload doesn't resurrect them; the fire's turn stays in Activity.
const DISMISSED_SCHEDULED_KEY = "protoagent.chat.dismissedScheduled";

function dismissedScheduledSet(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(DISMISSED_SCHEDULED_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function useDismissedScheduled() {
  const [dismissed, setDismissed] = useState<Set<string>>(dismissedScheduledSet);
  const dismiss = (key: string) => {
    const s = dismissedScheduledSet();
    s.add(key);
    try {
      localStorage.setItem(DISMISSED_SCHEDULED_KEY, JSON.stringify([...s].slice(-300)));
    } catch {
      /* storage unavailable — the card still hides for this page's lifetime */
    }
    setDismissed(s);
  };
  return { dismissed, dismiss };
}

/** Short local time for a fire's ISO timestamp ("2:00 PM"); falls back to the raw string. */
function fmtFiredAt(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** Collapse a result to one line — whitespace squeezed, truncated for the chip. */
function oneLine(text: string, max = 90): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max).trimEnd()}…` : flat;
}

// The scheduled-task RESULT CARD (#2990) — parallel to BackgroundReportCard, but a
// distinct visual: a calendar/clock icon (not FileText) and a full card body carrying the
// job id, fire time, result summary, and a "View full result" link into the Activity turn.
// Rendered for the FIRST fire in a session; recurring re-fires collapse to ScheduledChip.
function ScheduledReportCard({
  message,
  scheduled,
}: {
  message: ChatMessage;
  scheduled: NonNullable<ChatMessage["scheduled"]>;
}) {
  const { dismissed, dismiss } = useDismissedScheduled();
  const key = `${scheduled.jobId}:${scheduled.firedAt}`;
  if (dismissed.has(key)) return null;
  const failed = scheduled.status === "failed";
  const title = `Scheduled task ${scheduled.jobId} ${failed ? "failed" : "ran"}`;
  const summary = scheduled.summary?.trim() || "(no output)";
  const open = () =>
    openDocument({
      title,
      subtitle: `Fired ${fmtFiredAt(scheduled.firedAt)} — full turn in Activity`,
      load: () => Promise.resolve(summary),
    });
  return (
    <Message role={message.role} className="chat-report chat-scheduled">
      <div className={`chat-scheduled-card${failed ? " chat-scheduled-card--failed" : ""}`}>
        <div className="chat-scheduled-head">
          <CalendarClock size={14} aria-hidden />
          <span className="chat-scheduled-title" title={title}>
            {title}
          </span>
          <span className="chat-scheduled-time">{fmtFiredAt(scheduled.firedAt)}</span>
          <button
            type="button"
            className="chat-report-dismiss"
            onClick={() => dismiss(key)}
            title="Dismiss — the full turn stays in the Activity log"
            aria-label="Dismiss scheduled result"
          >
            <X size={14} />
          </button>
        </div>
        <div className="chat-scheduled-summary">{summary}</div>
        <div className="chat-scheduled-actions">
          <Button size="sm" variant="ghost" onClick={open}>
            View full result
          </Button>
        </div>
      </div>
    </Message>
  );
}

// The compact recurring variant (#2990): a second-or-later fire of the same schedule into
// this session renders as ONE dismissable line ("Scheduled task <id> ran — <summary>")
// instead of a full card, so an hourly check can't fill the chat with 24 cards/day.
function ScheduledChip({
  message,
  scheduled,
}: {
  message: ChatMessage;
  scheduled: NonNullable<ChatMessage["scheduled"]>;
}) {
  const { dismissed, dismiss } = useDismissedScheduled();
  const key = `${scheduled.jobId}:${scheduled.firedAt}`;
  if (dismissed.has(key)) return null;
  const failed = scheduled.status === "failed";
  const verb = failed ? "failed" : "ran";
  const line = `Scheduled task ${scheduled.jobId} ${verb} — ${oneLine(scheduled.summary || "(no output)")}`;
  return (
    <Message role={message.role} className="chat-report chat-scheduled-chip-msg">
      <div className="chat-report-chip">
        <CalendarClock size={14} aria-hidden />
        <span className="chat-report-title" title={line}>
          {line}
        </span>
        <button
          type="button"
          className="chat-report-dismiss"
          onClick={() => dismiss(key)}
          title="Dismiss — the full turn stays in the Activity log"
          aria-label="Dismiss scheduled result"
        >
          <X size={14} />
        </button>
      </div>
    </Message>
  );
}

/** Compact tokens (12340 → "12.3k", 1_200_000 → "1.2M"); raw under 1k. */
function fmtTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return `${(n / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M`;
}

/** Dollars: sub-cent gets 4 decimals so a fraction-of-a-cent turn still reads non-zero. */
function fmtCost(usd: number): string {
  if (usd === 0) return "$0";
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

/** Turn duration, matching the tool-card style: sub-second in ms, else one-decimal seconds. */
function fmtDuration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** One labelled row inside the hover tooltip. */
function TipRow({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="chat-usage-tip-row">
      <span className="chat-usage-tip-label">{label}</span>
      <span className="chat-usage-tip-value">
        {value}
        {sub ? <span className="chat-usage-tip-sub">{sub}</span> : null}
      </span>
    </div>
  );
}

/** Structured hover card: the full per-turn breakdown, honest about what each number means. */
function UsageTip({
  ctxTokens,
  threshold,
  context,
  usage,
  subscription,
}: {
  ctxTokens?: number;
  threshold?: number;
  context?: ContextWindow;
  usage?: TurnUsage;
  subscription: boolean;
}) {
  const compaction =
    context == null
      ? null
      : context.enabled === false
        ? "off"
        : threshold
          ? `near ${threshold.toLocaleString()} tokens${context.trigger ? ` · ${context.trigger}` : ""}`
          : context.trigger
            ? `${context.trigger} · no token threshold to chart`
            : null;
  return (
    <div className="chat-usage-tip">
      {ctxTokens != null ? (
        <TipRow label="Context" value={`${ctxTokens.toLocaleString()} tokens`} sub="this turn's prompt" />
      ) : null}
      {context?.projectedTokens != null ? (
        <TipRow
          label="Next request"
          value={`~${context.projectedTokens.toLocaleString()} tokens`}
          sub="the floor the next message starts from"
        />
      ) : null}
      {compaction ? <TipRow label="Compaction" value={compaction} /> : null}
      {usage ? <TipRow label="Output" value={`${usage.outputTokens.toLocaleString()} tokens`} /> : null}
      {usage?.cacheReadTokens ? (
        <TipRow label="Cache" value={`${usage.cacheReadTokens.toLocaleString()} tokens`} sub="reused from cache" />
      ) : null}
      {usage?.durationMs ? <TipRow label="Time" value={fmtDuration(usage.durationMs)} /> : null}
      {usage?.costUsd != null ? (
        <TipRow
          label={costTipLabel(subscription)}
          value={`${costPrefix(subscription)}${fmtCost(usage.costUsd)}`}
          sub={costTipSub(subscription)}
        />
      ) : null}
      <p className="chat-usage-tip-note">{usageTipNote(subscription)}</p>
    </div>
  );
}

/** The per-turn footer under an assistant answer: a context-window meter (fill ⊙, with a
 *  "/ threshold" bar when compaction is token-based) · output ↓ · cost. The full breakdown is
 *  a rich hover card. Honest about scope: `contextTokens` is the live prompt size; the cost is
 *  summed across the turn's calls (see ContextWindow / TurnUsage). */
function UsageFooter({ usage, context }: { usage?: TurnUsage; context?: ContextWindow }) {
  // Only the TRUE context-window fill (peak prompt). The old fallback to the turn's
  // summed input was the wrong axis entirely — a 38-call turn "sums" to millions while
  // the window sits at a fraction of that — so pre-context-v1 history now shows no
  // meter rather than a wrong one (#2787, ADR 0101 D6).
  const ctxTokens = context?.contextTokens;
  const threshold = context?.compactionAtTokens;
  const pct =
    ctxTokens != null && threshold ? Math.min(100, Math.round((ctxTokens / threshold) * 100)) : null;
  // Subscription-backed turns (#2463): dollars are an API-equivalent estimate, not a
  // charge — the footer and tip must say so. Cached app-wide; same key as the topbar.
  const { data: rt } = useQuery(runtimeStatusQuery());
  const subscription = isSubscriptionProvider(rt?.model?.provider);

  return (
    <Tooltip label={<UsageTip ctxTokens={ctxTokens} threshold={threshold} context={context} usage={usage} subscription={subscription} />} side="top" align="start">
      <div className="chat-usage">
        {ctxTokens != null ? (
          <span className="chat-usage-item" aria-label="context window">
            <Gauge size={13} aria-hidden />
            {fmtTokens(ctxTokens)}
            {threshold ? ` / ${fmtTokens(threshold)}` : ""}
            {pct != null ? (
              <span className="chat-usage-bar" aria-hidden>
                <span className="chat-usage-bar-fill" style={{ width: `${pct}%` }} data-warn={pct >= 80} />
              </span>
            ) : null}
          </span>
        ) : null}
        {usage ? (
          <span className="chat-usage-item" aria-label="output tokens">
            <ArrowDownToLine size={13} aria-hidden />
            {fmtTokens(usage.outputTokens)}
          </span>
        ) : null}
        {usage?.durationMs ? (
          <span className="chat-usage-item" aria-label="duration">
            <Clock size={13} aria-hidden />
            {fmtDuration(usage.durationMs)}
          </span>
        ) : null}
        {usage?.costUsd != null ? (
          <span className="chat-usage-item" aria-label={costAriaLabel(subscription)}>
            <Coins size={13} aria-hidden />
            {costPrefix(subscription)}
            {fmtCost(usage.costUsd)}
          </span>
        ) : null}
      </div>
    </Tooltip>
  );
}
