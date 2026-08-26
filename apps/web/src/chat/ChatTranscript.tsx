import { Empty } from "@protolabsai/ui/primitives";
import { Spinner } from "@protolabsai/ui/data";
import { Conversation, Message } from "@protolabsai/ui/ai";
import { TerminalSquare } from "lucide-react";
import { memo } from "react";

import type { ChatMessage } from "../lib/types";
import type { SessionStatus } from "./chat-store";
import { ChatMessageView, type ChatMessageActions } from "./ChatMessageView";
import { hideDismissedToolCalls } from "./dismissedToolCalls";

type ChatTranscriptProps = {
  sessionId: string;
  messages: ChatMessage[];
  dismissedToolCalls: Set<string>;
  actions: ChatMessageActions;
  steerQueue: { id: string; text: string }[];
  serverTurnLabel: string | null;
  status: SessionStatus;
  onCancelDelegation: (id: string) => void;
  onDismissToolCall: (id: string) => void;
  onCancelSteer: (id: string) => void;
};

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
  return (
    <Conversation id={`pl-conv-${sessionId}`}>
      {messages.length === 0 ? (
        <Empty icon={<TerminalSquare />} description="No messages in this session." />
      ) : (
        messages.map((message) => (
          <ChatMessageView
            key={message.id || `${message.role}-${message.createdAt}`}
            // Dismissed cards are stripped only for this client's view; the store and
            // backend history retain the complete turn.
            message={hideDismissedToolCalls(message, dismissedToolCalls)}
            onCancelDelegation={onCancelDelegation}
            onDismissToolCall={onDismissToolCall}
            actions={actions}
          />
        ))
      )}
      {steerQueue.map((queued) => (
        /* Optimistic steer bubble: cancellation either removes it before consumption or
           lets the authoritative stream settle it into the transcript. */
        <Message
          key={queued.id}
          role="user"
          queued
          queuedLabel="queued — folds into the agent's work at its next step"
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
