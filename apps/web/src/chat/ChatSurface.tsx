import {
  Loader2,
  MessageSquarePlus,
  MoreHorizontal,
  Send,
  Square,
  TerminalSquare,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../lib/api";
import type { ChatMessage } from "../lib/types";
import { chatStore, MAX_ACTIVE_SESSIONS, useChatState } from "./chat-store";
import { Markdown } from "./Markdown";

function messageId() {
  return `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function useSession(sessionId: string) {
  const state = useChatState();
  return state.sessions.find((session) => session.id === sessionId) || null;
}

export function ChatSurface({ onError }: { onError: (message: string) => void }) {
  const chat = useChatState();
  const currentSession = chat.sessions.find((session) => session.id === chat.currentSessionId) || null;

  useEffect(() => {
    if (!chat.currentSessionId && chat.sessions.length === 0) {
      chatStore.createSession();
    }
  }, [chat.currentSessionId, chat.sessions.length]);

  return (
    <section className="panel stage-panel chat-stage">
      <div className="chat-header">
        <div className="chat-title-group">
          <h1>Chat</h1>
          <p className="panel-kicker">
            {chat.activeSessions.length}/{MAX_ACTIVE_SESSIONS} mounted
          </p>
        </div>
        <button className="secondary-button" type="button" onClick={() => chatStore.createSession()}>
          <MessageSquarePlus size={15} />
          New
        </button>
      </div>

      <div className="chat-session-tabs" role="tablist" aria-label="Chat sessions">
        {chat.sessions.map((session) => {
          const active = session.id === chat.currentSessionId;
          const status = chat.sessionStatusMap[session.id] || "idle";
          return (
            <button
              className={active ? "active" : ""}
              type="button"
              role="tab"
              aria-selected={active}
              key={session.id}
              onClick={() => chatStore.switchSession(session.id)}
              title={session.title}
            >
              <span className={`session-dot ${status}`} />
              <span>{session.title}</span>
            </button>
          );
        })}
      </div>

      <div className="chat-session-pool">
        {chat.activeSessions.map((sessionId) => (
          <ChatSessionSlot
            key={sessionId}
            sessionId={sessionId}
            visible={sessionId === currentSession?.id}
            onError={onError}
          />
        ))}
      </div>
    </section>
  );
}

function ChatSessionSlot({
  sessionId,
  visible,
  onError,
}: {
  sessionId: string;
  visible: boolean;
  onError: (message: string) => void;
}) {
  const session = useSession(sessionId);
  const chat = useChatState();
  const [draft, setDraft] = useState("");
  const [statusMessage, setStatusMessage] = useState("");
  const [taskId, setTaskId] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const status = chat.sessionStatusMap[sessionId] || "idle";

  useEffect(() => {
    if (!visible) return;
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [session?.messages, visible]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  const messages = session?.messages || [];

  const canSend = useMemo(() => Boolean(draft.trim()) && status !== "streaming", [draft, status]);

  async function send() {
    if (!session || !canSend) return;
    const userMessage: ChatMessage = {
      id: messageId(),
      role: "user",
      content: draft.trim(),
      createdAt: Date.now(),
      status: "done",
    };
    const assistantId = messageId();
    const assistant: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      createdAt: Date.now(),
      status: "streaming",
    };

    setDraft("");
    setStatusMessage("submitted");
    chatStore.updateMessages(session.id, [...messages, userMessage, assistant]);
    chatStore.setSessionStatus(session.id, "streaming");
    onError("");

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await api.streamChat(userMessage.content, session.id, {
        signal: controller.signal,
        onTaskId: setTaskId,
        onStatus: setStatusMessage,
        onText: (text, append) => {
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) =>
              message.id === assistantId
                ? {
                    ...message,
                    content: append ? `${message.content}${text}` : text,
                    status: "streaming",
                  }
                : message,
            ),
          );
        },
        onDone: () => {
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) =>
              message.id === assistantId ? { ...message, status: "done" } : message,
            ),
          );
        },
      });
      chatStore.setSessionStatus(session.id, "idle");
      setStatusMessage("idle");
    } catch (exc) {
      if (controller.signal.aborted) {
        setStatusMessage("stopped");
      } else {
        const message = exc instanceof Error ? exc.message : String(exc);
        onError(message);
        setStatusMessage(message);
        chatStore.setSessionStatus(session.id, "error");
        const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
        if (latest) {
          chatStore.updateMessages(
            session.id,
            latest.messages.map((item) =>
              item.id === assistantId ? { ...item, content: item.content || message, status: "error" } : item,
            ),
          );
        }
        return;
      }
      chatStore.setSessionStatus(session.id, "idle");
    } finally {
      abortRef.current = null;
      setTaskId("");
    }
  }

  async function stop() {
    if (taskId) {
      try {
        await api.cancelTask(taskId);
      } catch {
        // The local abort below still releases the UI even if the task already finished.
      }
    }
    abortRef.current?.abort();
    chatStore.setSessionStatus(sessionId, "idle");
    setStatusMessage("stopped");
  }

  if (!session) return null;

  return (
    <div className="chat-session-slot" hidden={!visible}>
      <div className="panel-header chat-session-header">
        <div>
          <input
            className="session-title-input"
            value={session.title}
            onChange={(event) => chatStore.renameSession(session.id, event.target.value)}
            aria-label="Session title"
          />
          <p className="panel-kicker">{session.id}</p>
        </div>
        <div className="chat-session-actions">
          <StatusPill label={status === "streaming" ? statusMessage || "streaming" : status} tone={status === "error" ? "error" : status === "streaming" ? "warning" : "muted"} />
          <button className="icon-button" type="button" title="Session menu">
            <MoreHorizontal size={16} />
          </button>
          <button
            className="icon-button"
            type="button"
            title="Delete session"
            disabled={status === "streaming"}
            onClick={() => chatStore.deleteSession(session.id)}
          >
            <Trash2 size={16} />
          </button>
        </div>
      </div>

      <div className="message-list" ref={listRef}>
        {messages.length === 0 ? (
          <div className="empty-state">
            <TerminalSquare size={18} />
            <span>No messages in this session.</span>
          </div>
        ) : (
          messages.map((message) => (
            <article className={`message message-${message.role}`} key={message.id || `${message.role}-${message.createdAt}`}>
              <div className="message-role">{message.role}</div>
              <div className="message-body">
                {message.content
                  ? message.role === "assistant"
                    ? <Markdown>{message.content}</Markdown>
                    : message.content
                  : message.status === "streaming"
                    ? <Loader2 className="spin" size={15} />
                    : null}
              </div>
            </article>
          ))
        )}
      </div>

      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            // Cmd/Ctrl+Enter sends; plain Enter keeps inserting newlines.
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              void send();
            }
          }}
          placeholder="Message protoAgent  (⌘/Ctrl+Enter to send)"
          rows={3}
        />
        {status === "streaming" ? (
          <button className="secondary-button" type="button" onClick={() => void stop()}>
            <Square size={15} />
            Stop
          </button>
        ) : (
          <button className="primary-button" type="submit" disabled={!canSend}>
            <Send size={16} />
            Send
          </button>
        )}
      </form>
    </div>
  );
}

function StatusPill({ label, tone }: { label: string; tone: "warning" | "error" | "muted" }) {
  return <span className={`status-pill ${tone}`}>{label}</span>;
}
