import { Empty } from "@protolabsai/ui/primitives";
import { Spinner } from "@protolabsai/ui/data";
import { Conversation, Message } from "@protolabsai/ui/ai";
import { TerminalSquare } from "lucide-react";
import { memo, useMemo } from "react";

import type { ChatMessage, QueuedSteer } from "../lib/types";
import type { SessionStatus } from "./chat-store";
import { ChatMessageView, type ChatMessageActions } from "./ChatMessageView";
import { hideDismissedToolCalls } from "./dismissedToolCalls";

type ChatTranscriptProps = {
  sessionId: string;
  messages: ChatMessage[];
  dismissedToolCalls: Set<string>;
  actions: ChatMessageActions;
  /** Everything queued into a running turn — steers into this browser's stream AND
   *  interjections into an attended server turn (those carry `serverTaskId`). */
  steerQueue: QueuedSteer[];
  serverTurnLabel: string | null;
  status: SessionStatus;
  onCancelDelegation: (id: string) => void;
  onDismissToolCall: (id: string) => void;
  onCancelSteer: (id: string) => void;
};

type TranscriptMessageRowProps = {
  message: ChatMessage;
  dismissedToolCalls: Set<string>;
  actions: ChatMessageActions;
  onCancelDelegation: (id: string) => void;
  onDismissToolCall: (id: string) => void;
};

const TranscriptMessageRow = memo(function TranscriptMessageRow({
  message,
  dismissedToolCalls,
  actions,
  onCancelDelegation,
  onDismissToolCall,
}: TranscriptMessageRowProps) {
  // Filtering a dismissed card creates a derived message object. Retain that identity until
  // either its source row or the dismissal set changes, so another row's stream cannot make
  // this settled row expensive again.
  const visibleMessage = useMemo(
    () => hideDismissedToolCalls(message, dismissedToolCalls),
    [dismissedToolCalls, message],
  );
  return (
    <ChatMessageView
      message={visibleMessage}
      onCancelDelegation={onCancelDelegation}
      onDismissToolCall={onDismissToolCall}
      actions={actions}
    />
  );
});

// Keep the transcript outside the controlled composer's render path. In long chats the
// message tree contains expensive markdown, reasoning, and tool cards; a draft keystroke
// must not revisit that settled tree when none of these props changed (#3087).
export const ChatTranscript = memo(function ChatTranscript({
  sessionId,
  messages,
  dismissedToolCalls,
  actions,
  steerQueue,
  serverTurnLabel,
  status,
  onCancelDelegation,
  onDismissToolCall,
  onCancelSteer,
}: ChatTranscriptProps) {
  // The transcript is the one record of "settled": once a queued message's id is in it (the
  // agent consumed it and a marker or reconcile placed it), it is a normal user message and
  // must not ALSO render as pending. The slot prunes its queue to match; this keeps the one
  // render between the two from showing the message twice.
  const pending = useMemo(() => {
    if (!steerQueue.length) return steerQueue;
    const settled = new Set(messages.map((message) => message.id));
    return steerQueue.filter((queued) => !settled.has(queued.id));
  }, [messages, steerQueue]);
  return (
    <Conversation id={`pl-conv-${sessionId}`}>
      {messages.length === 0 ? (
        <Empty icon={<TerminalSquare />} description="No messages in this session." />
      ) : (
        messages.map((message) => (
          <TranscriptMessageRow
            key={message.id || `${message.role}-${message.createdAt}`}
            // Dismissed cards are stripped only for this client's view; the store and
            // backend history retain the complete turn.
            message={message}
            dismissedToolCalls={dismissedToolCalls}
            onCancelDelegation={onCancelDelegation}
            onDismissToolCall={onDismissToolCall}
            actions={actions}
          />
        ))
      )}
      {pending.map((queued) => (
        /* Optimistic queued bubble: cancellation either removes it before consumption or
           lets the server's consumed marker settle it into the transcript. A server-turn
           interjection is the same thing sent to a turn this browser isn't streaming — the
           server drains both from one steering queue — so it gets the same ✕. */
        <Message
          key={queued.id}
          role="user"
          queued
          queuedLabel={
            queued.serverTaskId
              ? "queued interjection — sent to this server turn"
              : "queued — folds into the agent's work at its next step"
          }
          onCancel={() => onCancelSteer(queued.id)}
        >
          <span className="chat-user-text">{queued.text}</span>
        </Message>
      ))}
      {serverTurnLabel && status !== "streaming" ? (
        /* A server-owned background turn cannot stream through this browser connection,
           so keep its activity visible until the resumed result arrives. */
        <Message role="assistant">
          <span className="chat-server-turn">
            <Spinner size={15} /> {serverTurnLabel}
          </span>
        </Message>
      ) : null}
    </Conversation>
  );
});
