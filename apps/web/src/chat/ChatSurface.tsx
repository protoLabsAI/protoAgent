import "./chat.css";
import { EditableText } from "@protolabsai/ui/forms";
import { Button, Empty } from "@protolabsai/ui/primitives";
import {
  Loader2,
  Plus,
  Send,
  Square,
  TerminalSquare,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../lib/api";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import type { ChatMessage, HitlPayload, SlashCommand, ToolCall } from "../lib/types";
import { HitlForm } from "./HitlForm";
import { notifyIfHidden } from "../lib/notify";
import { chatStore, useChatState } from "./chat-store";
import { Markdown } from "./LazyMarkdown";
import { ToolCalls } from "./ToolCalls";

function messageId() {
  return `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// Append an actionable pointer when a turn fails on something the operator can
// fix in the UI — chiefly model auth (a bad/blank API key 401s). Keeps the raw
// gateway detail (it's specific, e.g. "expected to start with 'sk-'") but tells
// the user where to fix it instead of leaving a cryptic error.
function withConfigHint(detail: string): string {
  const d = detail.toLowerCase();
  const looksAuth =
    d.includes("401") ||
    d.includes("403") ||
    d.includes("api key") ||
    d.includes("api_key") ||
    d.includes("auth") ||
    d.includes("virtual key") ||
    d.includes("sk-");
  if (looksAuth) {
    return `${detail}\n\n→ Check your model API key in **System → Settings** (or re-run setup), then “Test connection”.`;
  }
  return detail;
}

function useSession(sessionId: string) {
  const state = useChatState();
  return state.sessions.find((session) => session.id === sessionId) || null;
}

export function ChatSurface({
  onError,
  active = true,
}: {
  onError: (message: string) => void;
  // When false, the surface stays MOUNTED but hidden (display:none) — so an
  // in-flight turn keeps streaming into the store while the user is on another
  // tab, and returning shows the chat as if they never left. App renders this
  // unconditionally; only `active` toggles. (Matches protoMaker's always-mounted
  // chat overlay.)
  active?: boolean;
}) {
  const chat = useChatState();
  const currentSession = chat.sessions.find((session) => session.id === chat.currentSessionId) || null;
  const [editingId, setEditingId] = useState<string | null>(null);
  const [pendingClose, setPendingClose] = useState<string | null>(null);
  const pendingCloseSession = chat.sessions.find((s) => s.id === pendingClose) || null;

  useEffect(() => {
    if (!chat.currentSessionId && chat.sessions.length === 0) {
      chatStore.createSession();
    }
  }, [chat.currentSessionId, chat.sessions.length]);

  function closeSession(id: string) {
    // Retire server-side (harvest history → knowledge, purge checkpoints),
    // best-effort, then drop the tab locally.
    void api.deleteChatSession(id).catch(() => {});
    chatStore.deleteSession(id);
  }

  return (
    <section className="panel stage-panel chat-stage" style={active ? undefined : { display: "none" }} aria-hidden={!active}>
      {/* One row: a tab per session (status dot · title · close), then "+".
          Double-click a title to rename. Below ~26rem of container width it collapses
          to a session dropdown + add (the per-session strip can't fit) — container query. */}
      <div className="chat-tabbar-wrap">
      <div className="chat-tabbar" role="tablist" aria-label="Chat sessions">
        {chat.sessions.map((session) => {
          const active = session.id === chat.currentSessionId;
          const status = chat.sessionStatusMap[session.id] || "idle";
          return (
            <div className={`chat-tab ${active ? "active" : ""}`} role="tab" aria-selected={active} key={session.id}>
              <span className={`session-dot ${status}`} title={status} />
              {editingId === session.id ? (
                <EditableText
                  className="chat-tab-label"
                  inputClassName="chat-tab-edit"
                  value={session.title}
                  aria-label="Rename session"
                  editing
                  onEditingChange={(e) => setEditingId(e ? session.id : null)}
                  onCommit={(next) => chatStore.renameSession(session.id, next)}
                />
              ) : (
                <button
                  type="button"
                  className="chat-tab-label"
                  onClick={() => chatStore.switchSession(session.id)}
                  onDoubleClick={() => { chatStore.switchSession(session.id); setEditingId(session.id); }}
                  title={`${session.title} — double-click to rename`}
                >
                  {session.title}
                </button>
              )}
              <button
                type="button"
                className="chat-tab-close"
                title="Close session"
                aria-label={`Close ${session.title}`}
                onClick={() => setPendingClose(session.id)}
              >
                <X size={12} />
              </button>
            </div>
          );
        })}
        <button
          type="button"
          className="chat-tab-new"
          title="New chat"
          aria-label="New chat"
          onClick={() => chatStore.createSession()}
        >
          <Plus size={15} />
        </button>
      </div>

      {/* Narrow (container query) — the per-session strip collapses to a dropdown
          (switch) + close-current + add. Distinct classes so e2e's .chat-tab-* keep
          matching only the strip. */}
      <div className="chat-tabbar-compact">
        <select
          className="chat-session-select"
          value={chat.currentSessionId ?? ""}
          aria-label="Chat session"
          onChange={(e) => chatStore.switchSession(e.target.value)}
        >
          {chat.sessions.map((s) => (
            <option key={s.id} value={s.id}>{s.title}</option>
          ))}
        </select>
        <button
          type="button"
          className="chat-compact-btn"
          title="Close session"
          aria-label="Close current session"
          onClick={() => chat.currentSessionId && setPendingClose(chat.currentSessionId)}
        >
          <X size={12} />
        </button>
        <button
          type="button"
          className="chat-compact-btn"
          title="New chat"
          aria-label="New chat"
          onClick={() => chatStore.createSession()}
        >
          <Plus size={15} />
        </button>
      </div>
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

      <ConfirmDialog
        open={pendingClose !== null}
        title="Delete this chat?"
        confirmLabel="Delete chat"
        destructive
        onConfirm={() => {
          if (pendingClose) closeSession(pendingClose);
          setPendingClose(null);
        }}
        onClose={() => setPendingClose(null)}
      >
        {pendingCloseSession
          ? `"${pendingCloseSession.title}" and its history will be removed. The conversation is first harvested into the knowledge base, then its checkpoints are purged — this can't be undone from here.`
          : undefined}
      </ConfirmDialog>
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
  const [hitl, setHitl] = useState<HitlPayload | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const status = chat.sessionStatusMap[sessionId] || "idle";

  // Slash-command autocomplete. Commands the server handles (e.g. /goal) are
  // fetched once; the dropdown is active while typing "/name" (before a space).
  const [commands, setCommands] = useState<SlashCommand[]>([]);
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);

  useEffect(() => {
    api.chatCommands().then((r) => setCommands(r.commands)).catch(() => {});
  }, []);

  const slashQuery = useMemo(() => {
    if (slashDismissed || !draft.startsWith("/")) return null;
    const after = draft.slice(1);
    return after.includes(" ") ? null : after; // closes once a space is typed
  }, [draft, slashDismissed]);

  const slashMatches = useMemo(() => {
    if (slashQuery === null) return [];
    const q = slashQuery.toLowerCase();
    return commands.filter(
      (c) => !q || c.name.toLowerCase().includes(q) || c.description.toLowerCase().includes(q),
    );
  }, [slashQuery, commands]);

  const slashActive = slashMatches.length > 0;
  const slashSel = slashActive ? Math.min(slashIndex, slashMatches.length - 1) : 0;

  function completeCommand(cmd: SlashCommand) {
    setDraft(`/${cmd.name} `);
    setSlashIndex(0);
    setSlashDismissed(true); // a space follows, so it would close anyway
    textareaRef.current?.focus();
  }

  useEffect(() => {
    if (!visible) return;
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [session?.messages, visible]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  // Self-heal an interrupted turn (reload / network blip / a stale tab): if the
  // last assistant message is stuck in `streaming` with no live controller,
  // reconcile it against the server's durable task (A2A tasks/get) — finalize
  // when terminal, polling briefly if it's genuinely still running. Without this
  // an interrupted stream spins forever even though the server completed.
  useEffect(() => {
    if (abortRef.current) return; // a live turn in this slot owns the stream
    const snap = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
    const last = [...(snap?.messages || [])].reverse().find((m) => m.role === "assistant");
    if (!last || last.status !== "streaming" || !last.taskId || !last.id) return;

    const assistantId = last.id;
    const taskId = last.taskId;
    const TERMINAL = /completed|failed|canceled|cancelled/i;
    let cancelled = false;
    let polls = 0;
    const MAX_POLLS = 40; // ~2 min at 3s — then give up and leave it as-is

    function finalize(state: string, text: string) {
      const cur = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
      if (!cur) return;
      const failed = /fail|cancel/i.test(state);
      chatStore.updateMessages(
        sessionId,
        cur.messages.map((m) => {
          if (m.id !== assistantId) return m;
          const toolCalls = m.toolCalls?.map((c) => (c.status === "running" ? { ...c, status: "done" as const } : c));
          return { ...m, content: text || m.content, status: failed ? "error" : "done", toolCalls };
        }),
      );
      chatStore.setSessionStatus(sessionId, failed ? "error" : "idle");
    }

    async function tick() {
      if (cancelled) return;
      let res: { state: string; text: string };
      try {
        res = await api.getTask(taskId);
      } catch {
        return; // best-effort — leave the message as-is on a hard error
      }
      if (cancelled) return;
      if (!res.state || TERMINAL.test(res.state)) {
        // terminal, or the task is gone (un-stick rather than spin forever)
        finalize(res.state, res.text);
        return;
      }
      if (++polls < MAX_POLLS) setTimeout(tick, 3000);
    }
    void tick();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  const messages = session?.messages || [];

  const canSend = useMemo(() => Boolean(draft.trim()) && status !== "streaming", [draft, status]);

  async function send() {
    if (!session || !canSend) return;
    const content = draft.trim();
    setDraft("");
    void runTurn(content);
  }

  // Resume a paused (input-required) turn: submitting the HITL form/question
  // sends the response as a follow-up on the same session — the server feeds it
  // to the agent via Command(resume=…). A form response is serialized to JSON.
  async function resumeHitl(response: Record<string, unknown> | string) {
    setHitl(null);
    void runTurn(typeof response === "string" ? response : JSON.stringify(response));
  }

  async function runTurn(content: string) {
    if (!session || !content) return;
    const userMessage: ChatMessage = {
      id: messageId(),
      role: "user",
      content,
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
        onTaskId: (id) => {
          setTaskId(id);
          // Persist the task id on the assistant message so a stuck `streaming`
          // turn can be reconciled against the server task after a reload (below).
          const cur = chatStore.getSnapshot().sessions.find((s) => s.id === session.id);
          if (cur) {
            chatStore.updateMessages(
              session.id,
              cur.messages.map((m) => (m.id === assistantId ? { ...m, taskId: id } : m)),
            );
          }
        },
        onStatus: setStatusMessage,
        onFailed: (detail) => {
          // The turn failed terminally (e.g. the model 401'd on a bad key).
          // Surface it as an errored assistant message + an actionable hint,
          // instead of a silent "no response" with the error lost to the
          // transient status line.
          const friendly = withConfigHint(detail);
          onError(friendly);
          setStatusMessage("failed");
          chatStore.setSessionStatus(session.id, "error");
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (latest) {
            chatStore.updateMessages(
              session.id,
              latest.messages.map((item) =>
                item.id === assistantId ? { ...item, content: friendly, status: "error" } : item,
              ),
            );
          }
        },
        onInputRequired: (payload) => {
          setHitl(payload);
          // Alert natively if the window is hidden/unfocused (menu-bar-only
          // desktop, or a backgrounded tab) so the form isn't missed.
          notifyIfHidden(
            payload.title || "protoAgent needs your input",
            payload.question || payload.description,
          );
        },
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
        onToolCall: (evt) => {
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) => {
              if (message.id !== assistantId) return message;
              const calls = [...(message.toolCalls || [])];
              const idx = calls.findIndex((c) => c.id === evt.id);
              const now = Date.now();
              if (evt.phase === "start") {
                // A tool that starts while a `task` is still running is a child
                // of that subagent delegation — nest it. (Last open task wins,
                // so nested task() calls group correctly.)
                const openTask = [...calls]
                  .reverse()
                  .find((c) => c.name === "task" && c.status === "running" && c.id !== evt.id);
                const card: ToolCall = {
                  id: evt.id,
                  name: evt.name,
                  input: evt.input,
                  status: "running",
                  startedAt: now,
                  parentId: openTask?.id,
                };
                if (idx >= 0) calls[idx] = { ...calls[idx], ...card };
                else calls.push(card);
              } else {
                // end — flip the matching card to done (or create one if the
                // start frame was missed). Stamp elapsed when we saw the start.
                const startedAt = idx >= 0 ? calls[idx].startedAt : undefined;
                const durationMs = startedAt !== undefined ? now - startedAt : undefined;
                if (idx >= 0) {
                  calls[idx] = { ...calls[idx], output: evt.output, status: "done", durationMs };
                } else {
                  calls.push({ id: evt.id, name: evt.name, output: evt.output, status: "done" });
                }
              }
              return { ...message, toolCalls: calls };
            }),
          );
        },
        onDone: () => {
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          const now = Date.now();
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) => {
              if (message.id !== assistantId) return message;
              // A completed turn can't have tools still running: a tool_end frame
              // that races with the terminal `done` (e.g. a workflow card whose
              // end arrives in the same tick) would otherwise leave the card
              // spinning forever. Flip any lingering `running` cards to `done`.
              const toolCalls = message.toolCalls?.map((c) =>
                c.status === "running"
                  ? {
                      ...c,
                      status: "done" as const,
                      durationMs: c.durationMs ?? (c.startedAt !== undefined ? now - c.startedAt : undefined),
                    }
                  : c,
              );
              return { ...message, status: "done", toolCalls };
            }),
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
      <div className="message-list" ref={listRef}>
        {messages.length === 0 ? (
          <Empty icon={<TerminalSquare />} description="No messages in this session." />
        ) : (
          messages.map((message) => (
            <article className={`message message-${message.role}`} key={message.id || `${message.role}-${message.createdAt}`}>
              <div className="message-role">{message.role}</div>
              <div className="message-body">
                {message.toolCalls && message.toolCalls.length > 0 ? (
                  <ToolCalls calls={message.toolCalls} />
                ) : null}
                {message.content
                  ? message.role === "assistant"
                    ? <Markdown>{message.content}</Markdown>
                    : message.content
                  : message.status === "streaming" && !(message.toolCalls && message.toolCalls.length)
                    ? <Loader2 className="spin" size={15} />
                    : null}
              </div>
            </article>
          ))
        )}
      </div>

      {hitl && (
        <HitlForm
          payload={hitl}
          busy={status === "streaming"}
          onSubmit={resumeHitl}
          onCancel={() => setHitl(null)}
        />
      )}

      <div className="composer-wrap">
        {status === "streaming" && statusMessage ? (
          <div className="composer-status">
            <Loader2 className="spin" size={12} />
            <span>{statusMessage}</span>
          </div>
        ) : null}
        {slashActive ? (
          <div className="slash-menu" role="listbox">
            {slashMatches.map((cmd, index) => (
              <button
                type="button"
                key={cmd.name}
                role="option"
                aria-selected={index === slashSel}
                className={`slash-item${index === slashSel ? " active" : ""}`}
                onMouseEnter={() => setSlashIndex(index)}
                onClick={() => completeCommand(cmd)}
              >
                <span className="slash-name">/{cmd.name}</span>
                <span className="slash-desc">{cmd.usage || cmd.description}</span>
              </button>
            ))}
          </div>
        ) : null}
        <form
          className="composer"
          onSubmit={(event) => {
            event.preventDefault();
            void send();
          }}
        >
        <textarea
          ref={textareaRef}
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
            setSlashDismissed(false); // re-open the menu when the input changes
          }}
          onKeyDown={(event) => {
            // Slash-command navigation takes priority while the menu is open.
            if (slashActive) {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setSlashIndex((i) => (i + 1) % slashMatches.length);
                return;
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setSlashIndex((i) => (i - 1 + slashMatches.length) % slashMatches.length);
                return;
              }
              if (event.key === "Enter" || event.key === "Tab") {
                event.preventDefault();
                completeCommand(slashMatches[slashSel]);
                return;
              }
              if (event.key === "Escape") {
                event.preventDefault();
                setSlashDismissed(true);
                return;
              }
            }
            // Enter sends; Cmd/Ctrl+Enter (and Shift+Enter) insert a newline.
            if (event.key === "Enter") {
              if (event.metaKey || event.ctrlKey) {
                // Ctrl/Cmd+Enter → newline at the caret (the textarea wouldn't
                // insert one for this combo on its own).
                event.preventDefault();
                const ta = textareaRef.current;
                if (ta) {
                  const start = ta.selectionStart;
                  const end = ta.selectionEnd;
                  const next = `${draft.slice(0, start)}\n${draft.slice(end)}`;
                  setDraft(next);
                  requestAnimationFrame(() => {
                    ta.selectionStart = ta.selectionEnd = start + 1;
                  });
                }
                return;
              }
              if (!event.shiftKey) {
                // Plain Enter → send. (Shift+Enter falls through to a newline.)
                event.preventDefault();
                void send();
              }
            }
          }}
          placeholder="Message protoAgent  (/ for commands · Enter to send · ⌘/Ctrl+Enter for newline)"
          rows={3}
        />
        {status === "streaming" ? (
          <Button type="button" onClick={() => void stop()}>
            <Square size={15} />
            Stop
          </Button>
        ) : (
          <Button variant="primary" type="submit" disabled={!canSend}>
            <Send size={16} />
            Send
          </Button>
        )}
        </form>
      </div>
    </div>
  );
}

