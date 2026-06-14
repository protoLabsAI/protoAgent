import "./chat.css";
import { Empty } from "@protolabsai/ui/primitives";
import { PromptInput, Reasoning } from "@protolabsai/ui/ai";
import { TabBar } from "@protolabsai/ui/navigation";
import {
  Loader2,
  SlidersHorizontal,
  TerminalSquare,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import { runtimeStatusQuery } from "../lib/queries";
import { QuickSetting } from "../settings/QuickSetting";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import type { ChatMessage, HitlPayload, SlashCommand, ToolCall } from "../lib/types";
import { HitlForm } from "./HitlForm";
import { notifyIfHidden } from "../lib/notify";
import { chatStore, useChatState } from "./chat-store";
import { ChatComponent } from "./ChatComponent";
import { Markdown } from "./LazyMarkdown";
import { ToolCalls } from "./ToolCalls";

function messageId() {
  return `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// Read a File to bare base64 (no `data:…;base64,` prefix) — the proto Part `raw`
// (bytes) field for a native-vision image.
function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const s = String(reader.result || "");
      const comma = s.indexOf(",");
      resolve(comma >= 0 ? s.slice(comma + 1) : s);
    };
    reader.onerror = () => reject(reader.error || new Error("file read failed"));
    reader.readAsDataURL(file);
  });
}

// A file being attached to the next message. Uploaded to /api/knowledge/attach on
// pick; `context` is the backend's ready-to-prepend block (full text or lede).
type PendingAttachment = {
  id: string;
  name: string;
  kind: "file" | "image";
  status: "uploading" | "ready" | "error";
  context?: string;
  mode?: "inline" | "indexed";
  error?: string;
  // Native-vision images skip the pipeline: their base64 + mime ride the turn as
  // a multimodal A2A part straight to the model (no `context`).
  native?: boolean;
  b64?: string;
  mime?: string;
};

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
  const [pendingClose, setPendingClose] = useState<string | null>(null);
  const [harvestOnDelete, setHarvestOnDelete] = useState(false);
  const pendingCloseSession = chat.sessions.find((s) => s.id === pendingClose) || null;

  useEffect(() => {
    if (!chat.currentSessionId && chat.sessions.length === 0) {
      chatStore.createSession();
    }
  }, [chat.currentSessionId, chat.sessions.length]);

  function closeSession(id: string, harvest: boolean) {
    // Retire server-side (purge checkpoints; harvest into knowledge ONLY when
    // the dialog's checkbox opted in), best-effort, then drop the tab locally.
    void api.deleteChatSession(id, harvest).catch(() => {});
    chatStore.deleteSession(id);
  }

  return (
    <section className="panel stage-panel chat-stage" style={active ? undefined : { display: "none" }} aria-hidden={!active}>
      {/* DS TabBar (#832): a tab per session (status dot · title · close) + "+".
          Double-click a title to rename (TabBar owns the inline EditableText).
          `responsive` collapses to a DS-native <select> + add in a narrow panel
          (container query). The status dot rides the `icon` slot — wide-strip only:
          the collapsed <option> can't host markup, matching the old behavior. */}
      <TabBar
        ariaLabel="Chat sessions"
        responsive
        activeId={chat.currentSessionId ?? ""}
        items={chat.sessions.map((session) => {
          const status = chat.sessionStatusMap[session.id] || "idle";
          return {
            id: session.id,
            label: session.title,
            icon: <span className={`session-dot ${status}`} title={status} />,
          };
        })}
        onSelect={(id) => chatStore.switchSession(id)}
        onClose={(id) => setPendingClose(id)}
        onRename={(id, label) => chatStore.renameSession(id, label)}
        onAdd={() => chatStore.createSession()}
        addLabel="New chat"
      />

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
          if (pendingClose) closeSession(pendingClose, harvestOnDelete);
          setPendingClose(null);
          setHarvestOnDelete(false);
        }}
        onClose={() => { setPendingClose(null); setHarvestOnDelete(false); }}
      >
        {pendingCloseSession ? (
          <>
            <p style={{ margin: 0 }}>
              {`"${pendingCloseSession.title}" and its history will be removed — this can't be undone from here.`}
            </p>
            {/* Harvest is OPT-IN: deleting a chat must not silently copy it into
                searchable memory — the operator may be deleting it precisely to
                get rid of it. */}
            <label className="chat-delete-harvest">
              <input
                type="checkbox"
                checked={harvestOnDelete}
                onChange={(e) => setHarvestOnDelete(e.target.checked)}
              />
              Harvest into the knowledge base first (keeps a searchable summary)
            </label>
          </>
        ) : undefined}
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
  // Forwarded into the DS PromptInput (inputRef) — for slash-completion focus and
  // the Ctrl/⌘+Enter caret insert. The DS component owns the auto-grow.
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const status = chat.sessionStatusMap[sessionId] || "idle";

  // Pending file attachments. Each is uploaded to /api/knowledge/attach on pick;
  // the backend tiers it (inline small / index large) and returns a `context`
  // block we prepend to the SENT message (not the visible bubble) on send.
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  // Native vision: when the active model accepts images, attached images go
  // straight to the model as multimodal parts; otherwise they take the pipeline.
  const { data: runtime } = useQuery(runtimeStatusQuery());
  const visionModel = Boolean(runtime?.model?.vision);

  async function uploadAttachment(file: File) {
    const id = messageId();
    const kind: "file" | "image" = file.type.startsWith("image/") ? "image" : "file";
    setAttachments((a) => [...a, { id, name: file.name, kind, status: "uploading" }]);

    // Native vision: a vision model sees the image directly — base64 it and send
    // it as a multimodal part, no pipeline round-trip.
    if (kind === "image" && visionModel) {
      try {
        const b64 = await fileToBase64(file);
        setAttachments((a) =>
          a.map((x) =>
            x.id === id
              ? { ...x, status: "ready", native: true, b64, mime: file.type || "image/png", mode: "inline" }
              : x,
          ),
        );
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setAttachments((a) => a.map((x) => (x.id === id ? { ...x, status: "error", error: msg } : x)));
        onError(`Couldn't read ${file.name}: ${msg}`);
      }
      return;
    }

    try {
      const form = new FormData();
      form.append("file", file);
      form.append("session_id", sessionId);
      const r = await api.attachToChat(form);
      if (!r.enabled || !r.context) throw new Error("attachment not accepted");
      setAttachments((a) =>
        a.map((x) => (x.id === id ? { ...x, status: "ready", context: r.context, mode: r.mode } : x)),
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setAttachments((a) => a.map((x) => (x.id === id ? { ...x, status: "error", error: msg } : x)));
      onError(`Couldn't attach ${file.name}: ${msg}`);
    }
  }

  function removeAttachment(id: string) {
    setAttachments((a) => a.filter((x) => x.id !== id));
  }

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

  // Runs BEFORE the DS PromptInput's Enter-to-submit (via its onKeyDown seam):
  // preventDefault to take over the key. Slash-menu nav wins while open; ⌘/Ctrl+Enter
  // inserts a newline. Plain Enter falls through → PromptInput submits (→ send()).
  function onComposerKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
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
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      // ⌘/Ctrl+Enter → newline at the caret (the textarea wouldn't on its own).
      event.preventDefault();
      const ta = textareaRef.current;
      if (ta) {
        const start = ta.selectionStart;
        const end = ta.selectionEnd;
        setDraft(`${draft.slice(0, start)}\n${draft.slice(end)}`);
        requestAnimationFrame(() => {
          ta.selectionStart = ta.selectionEnd = start + 1;
        });
      }
    }
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
    const text = draft.trim();
    setDraft("");
    // Native-vision images ride the turn as multimodal parts; pipeline attachments
    // contribute a prepended context block.
    const nativeImgs = attachments.filter((a) => a.status === "ready" && a.native && a.b64);
    const piped = attachments.filter((a) => a.status === "ready" && a.context);
    if (nativeImgs.length === 0 && piped.length === 0) {
      void runTurn(text);
      return;
    }
    const images = nativeImgs.map((a) => ({ b64: a.b64 as string, mime: a.mime || "image/png", name: a.name }));
    // The model gets the pipeline context prepended + the images natively; the
    // user bubble shows only the typed text + a 📎 list (never a raw doc/data dump).
    const sent = [...piped.map((a) => a.context as string), text].join("\n\n").trim();
    const names = [...piped, ...nativeImgs].map((a) => a.name).join(", ");
    const display = text ? `${text}\n\n📎 ${names}` : `📎 ${names}`;
    setAttachments([]);
    void runTurn(display, { sendAs: sent, images });
  }

  // Resume a paused (input-required) turn: submitting the HITL form/question
  // sends the response as a follow-up on the same session — the server feeds it
  // to the agent via Command(resume=…). A form response is serialized to JSON.
  async function resumeHitl(response: Record<string, unknown> | string) {
    // An approval gate (Approve/Deny on, e.g., run_command) isn't conversation — resume
    // the turn but DON'T append an "approved"/"denied" user bubble. The outcome lives on
    // the tool card itself (running → done on approve, error on deny), so the bubble is
    // just noise. A form/question answer IS meaningful content, so those stay visible.
    const silent = hitl?.kind === "approval";
    setHitl(null);
    void runTurn(typeof response === "string" ? response : JSON.stringify(response), { hidden: silent });
  }

  async function runTurn(
    content: string,
    opts: { hidden?: boolean; sendAs?: string; images?: { b64: string; mime: string; name: string }[] } = {},
  ) {
    if (!session || !content) return;
    // `sendAs` (attachment context prepended) is what the MODEL receives; `content`
    // is what the user bubble shows.
    const sent = opts.sendAs ?? content;
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
    // `hidden` (an approval resume) sends `content` to the server but omits the user
    // bubble — the agent still receives the approve/deny, the chat just doesn't show it.
    chatStore.updateMessages(
      session.id,
      opts.hidden ? [...messages, assistant] : [...messages, userMessage, assistant],
    );
    chatStore.setSessionStatus(session.id, "streaming");
    onError("");

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await api.streamChat(sent, session.id, {
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
        onReasoning: (delta) => {
          // Accumulate the streamed scratch_pad into the assistant message's
          // collapsible reasoning block (separate from the answer text).
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) =>
              message.id === assistantId
                ? { ...message, reasoning: `${message.reasoning ?? ""}${delta}` }
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
                // end — flip the matching card to done/error (or create one if the
                // start frame was missed). A failed end (e.g. a declined run_command)
                // closes the card as an error (X). Stamp elapsed when we saw the start.
                const startedAt = idx >= 0 ? calls[idx].startedAt : undefined;
                const durationMs = startedAt !== undefined ? now - startedAt : undefined;
                const endStatus = evt.error ? "error" : "done";
                if (idx >= 0) {
                  calls[idx] = { ...calls[idx], output: evt.output, status: endStatus, durationMs };
                } else {
                  calls.push({ id: evt.id, name: evt.name, output: evt.output, status: endStatus });
                }
              }
              return { ...message, toolCalls: calls };
            }),
          );
        },
        onComponent: (spec) => {
          // A renderable component (ADR 0051) — append to the assistant message; the
          // registry renders it inline after the text.
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) =>
              message.id === assistantId
                ? { ...message, components: [...(message.components || []), spec] }
                : message,
            ),
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
      }, { images: opts.images });
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
                {message.reasoning ? (
                  // Collapsible "thinking" — open while the model is still reasoning
                  // (no answer text yet), auto-collapses once the answer starts.
                  <Reasoning streaming={message.status === "streaming" && !message.content}>
                    {message.reasoning}
                  </Reasoning>
                ) : null}
                {message.toolCalls && message.toolCalls.length > 0 ? (
                  <ToolCalls calls={message.toolCalls} />
                ) : null}
                {message.content
                  ? message.role === "user"
                    ? message.content
                    : // assistant + system (e.g. background-completion notifications,
                      // ADR 0050) carry markdown — render it; only the user's own input
                      // stays literal.
                      <Markdown>{message.content}</Markdown>
                  : message.status === "streaming"
                      && !(message.toolCalls && message.toolCalls.length)
                      && !(message.components && message.components.length)
                      && !message.reasoning
                    ? <Loader2 className="spin" size={15} />
                    : null}
                {message.components && message.components.length > 0
                  ? message.components.map((spec, i) => <ChatComponent key={i} spec={spec} />)
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

      <div
        className="composer-wrap"
        onMouseDown={(e) => {
          // Click anywhere in the prompt box (its padding / button bar) focuses the
          // field — not just the textarea. Skip when the click is outside the box or
          // on an interactive child (send/stop button, slash item, the field itself).
          const target = e.target as HTMLElement;
          if (!target.closest(".pl-prompt")) return;
          if (target.closest("button, a, textarea, input, select, label, [role='option']")) return;
          e.preventDefault(); // keep focus from leaving the field
          textareaRef.current?.focus();
        }}
        onDragOver={(e) => { if (e.dataTransfer?.types?.includes("Files")) e.preventDefault(); }}
        onDrop={(e) => {
          const files = Array.from(e.dataTransfer?.files ?? []);
          if (files.length) {
            e.preventDefault();
            files.forEach((f) => void uploadAttachment(f));
          }
        }}
      >
        {status === "streaming" && statusMessage ? (
          <div className="composer-status">
            <Loader2 className="spin" size={12} />
            <span>{statusMessage}</span>
          </div>
        ) : null}
        <PromptInput
          value={draft}
          onChange={(v) => {
            setDraft(v);
            setSlashDismissed(false); // re-open the menu when the input changes
          }}
          // The DS button is Send when idle, Stop (square) while streaming.
          onSubmit={() => {
            if (status === "streaming") void stop();
            else void send();
          }}
          loading={status === "streaming"}
          placeholder="Message protoAgent  (/ for commands · Enter to send · ⌘/Ctrl+Enter for newline)"
          inputRef={textareaRef}
          onKeyDown={onComposerKeyDown}
          onPaste={(e) => {
            // Paste-to-attach (the DS onPaste seam): files on the clipboard become
            // attachments; plain text paste falls through to the field.
            const files = Array.from(e.clipboardData?.files ?? []);
            if (files.length) {
              e.preventDefault();
              files.forEach((f) => void uploadAttachment(f));
            }
          }}
          onAttach={() => fileInputRef.current?.click()}
          attachments={attachments.map((a) => ({
            id: a.id,
            name: a.name,
            kind: a.kind,
            size:
              a.status === "uploading" ? "uploading…"
              : a.status === "error" ? "failed"
              : a.mode === "indexed" ? "indexed for retrieval"
              : undefined,
          }))}
          onRemoveAttachment={removeAttachment}
          overlay={slashActive ? (
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
        />
        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          accept={
            ".txt,.text,.log,.csv,.md,.markdown,.html,.htm,.pdf," +
            ".png,.jpg,.jpeg,.gif,.webp,.bmp," +
            ".mp3,.wav,.m4a,.flac,.ogg,.opus,.aac,.mp4,.mov,.mkv,.webm,.avi,.m4v"
          }
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            files.forEach((f) => void uploadAttachment(f));
            e.target.value = ""; // allow re-picking the same file
          }}
        />
        {/* Model control, under the input (ADR 0048) — a chip showing the active model
            alias; click to tune model / temperature / max tokens. Same field + cascade
            as Settings ▸ Workspace ▸ Settings. */}
        <div className="composer-toolbar">
          <QuickSetting
            keys={["model.name", "model.temperature", "model.max_tokens"]}
            summaryKey="model.name"
            title="Model"
            label="Model settings"
            icon={<SlidersHorizontal size={14} />}
          />
        </div>
      </div>
    </div>
  );
}

