import { Button } from "@protolabsai/ui/primitives";
import { Message, MessageAction, MessageActions } from "@protolabsai/ui/ai";
import { Check, Copy, GitBranch, Loader2, Maximize2, RotateCcw } from "lucide-react";

import { openDocument } from "../docviewer";
import { api } from "../lib/api";
import type { ChatMessage, ChatPart } from "../lib/types";
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
  return (
    <Message role={message.role} streaming={streaming} className={message.report ? "chat-report" : undefined}>
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
          let split = parts.length;
          while (split > 0 && parts[split - 1].kind === "text") split--;
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
            ) : (
              renderText(part, `w${i}`)
            );
          return (
            <>
              {hasTools && hasReasoning ? (
                <WorkBlock parts={workParts} toolCalls={message.toolCalls} streaming={streaming && answerParts.length === 0} />
              ) : (
                workParts.map(renderInline)
              )}
              {answerParts.map((part, i) => renderText(part, `a${i}`))}
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
      {message.components && message.components.length > 0
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
