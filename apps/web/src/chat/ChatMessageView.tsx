import { Button } from "@protolabsai/ui/primitives";
import { Message, MessageAction, MessageActions } from "@protolabsai/ui/ai";
import { Tooltip } from "@protolabsai/ui/overlays";
import { ArrowDownToLine, Check, Clock, Coins, Copy, GitBranch, Gauge, Loader2, Maximize2, RotateCcw } from "lucide-react";

import { openDocument } from "../docviewer";
import { api } from "../lib/api";
import { useUI } from "../state/uiStore";
import type { ChatMessage, ChatPart, ContextWindow, TurnUsage } from "../lib/types";
import { ChatComponent } from "./ChatComponent";
import { Markdown } from "./LazyMarkdown";
import { ReasoningCard } from "./ReasoningCard";
import { ToolCalls } from "./ToolCalls";
import { WorkBlock } from "./WorkBlock";
import { toolsForGroup } from "./parts";

// Optional per-message action row (copy / fork / regenerate). Omit it (e.g. the ⌘K palette
// chat) and no actions render. Each callback is independently optional.
export type ChatMessageActions = {
  copiedId?: string | null;
  onCopy?: (m: ChatMessage) => void;
  onFork?: (m: ChatMessage) => void;
  onRegenerate?: (id: string) => void;
  lastAssistantId?: string;
  regenDisabled?: boolean;
};

// The single chat message renderer (ADR 0035) — shared by the main chat (ChatSurface) and the
// ⌘K palette chat (PaletteChat) so they never drift. Renders one user/assistant/system message:
// live ordered `parts` (text↔tool interleave, WorkBlock fold) or the history-loaded grouped
// fallback, plus the streaming loader, inline components, the background-report card, and the
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
  return (
    <Message
      role={message.role}
      streaming={streaming}
      className={
        message.report
          ? "chat-report"
          : // Any non-report system message is a local note → compact .chat-note card; the
            // tone modifier is appended only when set, so neutral notes still get the styling.
            message.role === "system"
            ? `chat-note${message.noteTone ? ` chat-note--${message.noteTone}` : ""}`
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
          // The "answer" is the trailing run of text AND component parts — a component is
          // emitted before its summary text, so it belongs with the answer (above the text),
          // not folded into the work timeline (#1323). Everything before is the work.
          let split = parts.length;
          while (split > 0 && (parts[split - 1].kind === "text" || parts[split - 1].kind === "component")) split--;
          const workParts = parts.slice(0, split);
          const answerParts = parts.slice(split);
          const hasTools = workParts.some((p) => p.kind === "tools");
          const hasReasoning = workParts.some((p) => p.kind === "reasoning");
          const renderText = (part: ChatPart, key: string) =>
            part.kind !== "text" || !part.text.trim() ? null : message.role === "user" ? (
              <span className="chat-user-text" key={key}>{part.text}</span>
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
              {hasTools && hasReasoning ? (
                <WorkBlock parts={workParts} toolCalls={message.toolCalls} streaming={streaming && answerParts.length === 0} />
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
              <span className="chat-user-text">{message.content}</span>
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
        <Loader2 className="spin" size={15} />
      ) : null}
      {/* History fallback: a message persisted before component-parts existed renders its
          components here (after the answer). Live turns render them inline via ordered parts
          above — so skip this when any component part is present, to avoid double-rendering. */}
      {message.components && message.components.length > 0 && !message.parts?.some((p) => p.kind === "component")
        ? message.components.map((spec, i) => <ChatComponent key={i} spec={spec} />)
        : null}
      {/* Background-agent report (ADR 0050/0062): the bubble shows the server's preview; this
          opens the FULL report in the full-screen document viewer (fetched by job id). */}
      {message.report ? (
        <Button
          className="chat-report-open"
          variant="ghost"
          size="sm"
          onClick={() =>
            openDocument({
              title: message.report!.title,
              subtitle: "Background agent report",
              load: () =>
                api
                  .background()
                  .then(
                    (r) =>
                      r.jobs.find((j) => j.id === message.report!.jobId)?.result ||
                      "_The full report is no longer available — it may have been cleared from the Background agents panel._",
                  ),
            })
          }
        >
          <Maximize2 size={14} /> Read full report
        </Button>
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
          {actions.onRegenerate && message.id === actions.lastAssistantId ? (
            <MessageAction
              label="Regenerate"
              icon={<RotateCcw size={14} />}
              disabled={actions.regenDisabled}
              onClick={() => actions.onRegenerate!(message.id!)}
            />
          ) : null}
        </MessageActions>
      ) : null}
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
}: {
  ctxTokens?: number;
  threshold?: number;
  context?: ContextWindow;
  usage?: TurnUsage;
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
      {compaction ? <TipRow label="Compaction" value={compaction} /> : null}
      {usage ? <TipRow label="Output" value={`${usage.outputTokens.toLocaleString()} tokens`} /> : null}
      {usage?.cacheReadTokens ? (
        <TipRow label="Cache" value={`${usage.cacheReadTokens.toLocaleString()} tokens`} sub="reused from cache" />
      ) : null}
      {usage?.durationMs ? <TipRow label="Time" value={fmtDuration(usage.durationMs)} /> : null}
      {usage?.costUsd != null ? <TipRow label="Cost" value={fmtCost(usage.costUsd)} /> : null}
      <p className="chat-usage-tip-note">Context is the live prompt size; cost is summed across the turn's calls.</p>
    </div>
  );
}

/** The per-turn footer under an assistant answer: a context-window meter (fill ⊙, with a
 *  "/ threshold" bar when compaction is token-based) · output ↓ · cost. The full breakdown is
 *  a rich hover card. Honest about scope: `contextTokens` is the live prompt size; the cost is
 *  summed across the turn's calls (see ContextWindow / TurnUsage). */
function UsageFooter({ usage, context }: { usage?: TurnUsage; context?: ContextWindow }) {
  // Prefer the true context-window fill (peak prompt); fall back to the summed input only
  // for history saved before context-v1 shipped.
  const ctxTokens = context?.contextTokens ?? usage?.inputTokens;
  const threshold = context?.compactionAtTokens;
  const pct =
    ctxTokens != null && threshold ? Math.min(100, Math.round((ctxTokens / threshold) * 100)) : null;

  return (
    <Tooltip label={<UsageTip ctxTokens={ctxTokens} threshold={threshold} context={context} usage={usage} />} side="top" align="start">
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
          <span className="chat-usage-item" aria-label="cost">
            <Coins size={13} aria-hidden />
            {fmtCost(usage.costUsd)}
          </span>
        ) : null}
      </div>
    </Tooltip>
  );
}
