import "./chat.css";
import { Empty } from "@protolabsai/ui/primitives";
import { Switch } from "@protolabsai/ui/forms";
import {
  Conversation,
  Message,
  MessageAction,
  MessageActions,
  PromptInput,
  Reasoning,
} from "@protolabsai/ui/ai";
import { TabBar } from "@protolabsai/ui/navigation";
import {
  BookOpen,
  Check,
  Copy,
  GitBranch,
  Loader2,
  RotateCcw,
  TerminalSquare,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { runtimeStatusQuery } from "../lib/queries";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import type { ChatMessage, HitlPayload, SlashCommand, ToolCall } from "../lib/types";
import { HitlForm } from "./HitlForm";
import { notifyIfHidden } from "../lib/notify";
import {
  chatStore,
  useChatState,
  DEFAULT_REASONING_EFFORT,
  REASONING_EFFORTS,
  effectiveReasoningEffort,
} from "./chat-store";
import { ChatComponent } from "./ChatComponent";
import { ComposerModelSelect } from "./ComposerModelSelect";
import { Markdown } from "./LazyMarkdown";
import { filesFromTransfer, isLargePaste, pastedTextFile } from "./paste";
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
        onReorder={(next) => chatStore.reorderSessions(next.map((t) => t.id))}
        onAdd={() => chatStore.createSession()}
        addLabel="New chat"
      />

      <div className="chat-session-pool">
        {chat.activeSessions.map((sessionId) => (
          <ChatSessionSlot
            key={sessionId}
            sessionId={sessionId}
            visible={sessionId === currentSession?.id}
            surfaceActive={active}
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
            <Switch
              className="chat-delete-harvest"
              checked={harvestOnDelete}
              onCheckedChange={setHarvestOnDelete}
              label="Harvest into the knowledge base first (keeps a searchable summary)"
            />
          </>
        ) : undefined}
      </ConfirmDialog>
    </section>
  );
}

function ChatSessionSlot({
  sessionId,
  visible,
  surfaceActive,
  onError,
}: {
  sessionId: string;
  visible: boolean;
  // The chat SURFACE is the active rail surface (not just: this is the active session
  // tab). Both must be true for the composer to grab focus.
  surfaceActive: boolean;
  onError: (message: string) => void;
}) {
  const session = useSession(sessionId);
  const chat = useChatState();
  const [draft, setDraft] = useState("");
  const [statusMessage, setStatusMessage] = useState("");
  const [taskId, setTaskId] = useState("");
  const [hitl, setHitl] = useState<HitlPayload | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Transient "copied ✓" feedback on a message's copy action.
  const [copiedId, setCopiedId] = useState<string | null>(null);
  // Mid-turn steering: user messages queued WHILE a turn streams (optimistic),
  // reconciled at turn-end. The ref mirrors the state so the post-stream reconcile
  // (a stale render closure) reads the live queue.
  const [steerQueue, setSteerQueueState] = useState<{ id: string; text: string }[]>([]);
  const steerQueueRef = useRef<{ id: string; text: string }[]>([]);
  const setSteerQueue = (next: { id: string; text: string }[]) => {
    steerQueueRef.current = next;
    setSteerQueueState(next);
  };
  // Forwarded into the DS PromptInput (inputRef) — for slash-completion focus and
  // the Ctrl/⌘+Enter caret insert. The DS component owns the auto-grow.
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // Autofocus the composer when this becomes the active session AND the chat surface is
  // the active rail surface — so clicking the Chat rail item (or switching tabs) lands
  // focus in the composer without a click. (`visible` alone is the active tab, which
  // doesn't change when you switch INTO the chat surface from another rail item.)
  useEffect(() => {
    if (visible && surfaceActive) textareaRef.current?.focus();
  }, [visible, surfaceActive]);
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
        const msg = errMsg(e);
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
      const msg = errMsg(e);
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
    // Deterministic client-side commands (ADR 0057) surface first, then server skills.
    const all: SlashCommand[] = [
      { name: "new", description: "Open a new chat tab" },
      { name: "clear", description: "Clear this chat's history" },
      { name: "effort", description: "Reasoning effort: low | medium | high | max | off" },
      ...commands,
    ];
    return all.filter(
      (c) => !q || c.name.toLowerCase().includes(q) || c.description.toLowerCase().includes(q),
    );
  }, [slashQuery, commands]);

  const slashActive = slashMatches.length > 0;
  const slashSel = slashActive ? Math.min(slashIndex, slashMatches.length - 1) : 0;

  // A local-only system note in the thread (e.g. /effort confirmation) — never sent
  // to the agent, just shown so the operator sees the command took effect.
  function noteToThread(text: string) {
    if (!session) return;
    const base = chatStore.getSnapshot().sessions.find((s) => s.id === session.id)?.messages ?? [];
    chatStore.updateMessages(session.id, [
      ...base,
      { id: messageId(), role: "assistant", content: text, createdAt: Date.now(), status: "done" },
    ]);
  }

  // Deterministic client-side commands (ADR 0057) — run locally, never sent to the
  // agent. `/new` opens + focuses a fresh tab; `/clear` wipes THIS tab's history
  // (server checkpoint + transcript), keeping the tab; `/effort <level>` sets this
  // tab's reasoning effort (sent with each turn). `raw` is the command minus the
  // slash, e.g. "effort high". Returns true if handled.
  function runClientSlash(raw: string): boolean {
    const [verb, ...rest] = raw.split(/\s+/);
    const arg = rest.join(" ").trim().toLowerCase();
    if (verb === "new") {
      chatStore.createSession();
      textareaRef.current?.focus();
      return true;
    }
    if (verb === "clear" && session) {
      void api.deleteChatSession(session.id, false).catch(() => {});
      chatStore.updateMessages(session.id, []);
      textareaRef.current?.focus();
      return true;
    }
    if (verb === "effort" && session) {
      const opts = REASONING_EFFORTS.join(" · ");
      if (!arg) {
        const cur = session.reasoningEffort ?? `${DEFAULT_REASONING_EFFORT} (default)`;
        noteToThread(`⚙ Reasoning effort: **${cur}**. Set it with \`/effort ${REASONING_EFFORTS.join("|")}\`.`);
      } else if ((REASONING_EFFORTS as readonly string[]).includes(arg)) {
        chatStore.setSessionReasoningEffort(session.id, arg);
        const off = arg === "off" ? " — reasoning disabled for this tab" : "";
        noteToThread(`⚙ Reasoning effort set to **${arg}**${off}. Applies to the next message.`);
      } else {
        noteToThread(`⚠ Unknown effort \`${arg}\`. Options: ${opts}.`);
      }
      textareaRef.current?.focus();
      return true;
    }
    return false;
  }

  function completeCommand(cmd: SlashCommand) {
    // A client command runs on pick; a server skill fills the draft to edit + send.
    if (runClientSlash(cmd.name)) {
      setDraft("");
      setSlashIndex(0);
      setSlashDismissed(true);
      return;
    }
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
  // Regenerate is offered only on the most recent assistant reply.
  const lastAssistantId = useMemo(
    () => [...messages].reverse().find((m) => m.role === "assistant")?.id,
    [messages],
  );

  // Sendable with text OR at least one ready attachment (file-only send, e.g.
  // "describe this image" with no caption). Matches the DS PromptInput gate,
  // which also enables submit when attachments are present (@protolabsai/ui ≥ 0.34).
  const canSend = useMemo(
    () =>
      (Boolean(draft.trim()) || attachments.some((a) => a.status === "ready")) &&
      status !== "streaming",
    [draft, attachments, status],
  );

  async function send() {
    if (!session || !canSend) return;
    const text = draft.trim();
    setDraft("");
    // Deterministic client-side slash commands (ADR 0057) — handled locally, not sent.
    if (text.startsWith("/") && runClientSlash(text.slice(1).trim())) return;
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

  // Steer a RUNNING turn: queue the typed message (folds in at the agent's next
  // model call via SteeringMiddleware) without stopping the stream. Shows an
  // optimistic "queued" bubble; turn-end reconcile settles or re-sends it.
  async function queueSteer() {
    const text = draft.trim();
    if (!session || !text) return;
    const id = messageId();
    setDraft("");
    setSteerQueue([...steerQueueRef.current, { id, text }]);
    try {
      await api.steerChat(session.id, id, text);
    } catch (e) {
      setSteerQueue(steerQueueRef.current.filter((x) => x.id !== id));
      onError(`Couldn't queue message: ${errMsg(e)}`);
    }
  }

  // Cancel a queued steer via the ✕ on its pending bubble. Drop the bubble
  // optimistically so the click feels instant, then DELETE it server-side. If the
  // agent had already drained it (`removed: false`), it's too late to cancel — it
  // shaped the reply, so settle it into the thread instead of lying it never ran.
  async function cancelSteer(id: string) {
    if (!session) return;
    const item = steerQueueRef.current.find((q) => q.id === id);
    if (!item) return;
    setSteerQueue(steerQueueRef.current.filter((q) => q.id !== id));
    try {
      const { removed } = await api.cancelSteer(session.id, id);
      if (!removed) settleConsumed([item]);
    } catch (e) {
      // Couldn't reach the backend — restore the bubble rather than drop a steer
      // that may still be queued (avoid concurrent-add clobber by re-checking).
      if (!steerQueueRef.current.some((q) => q.id === id)) {
        setSteerQueue([...steerQueueRef.current, item]);
      }
      onError(`Couldn't cancel message: ${errMsg(e)}`);
    }
  }

  // Tier 2: abort a running subagent delegation (the Stop on a running `task` tool
  // card). Cancels just that delegation server-side — the lead continues; the card
  // settles to done with a "cancelled" result via the normal tool_end stream, so we
  // don't mutate it here. Distinct from the composer Stop, which kills the whole turn.
  async function cancelDelegation(delegationId: string) {
    if (!session) return;
    try {
      await api.cancelDelegation(session.id, delegationId);
    } catch (e) {
      onError(`Couldn't cancel delegation: ${errMsg(e)}`);
    }
  }

  // Settle steered messages the agent has folded in: drop them from the queue and
  // place them into the thread just before the turn's current assistant message —
  // they shaped it. Shared by the mid-turn poll and the turn-end reconcile.
  function settleConsumed(consumed: { id: string; text: string }[]) {
    if (!session || !consumed.length) return;
    const consumedIds = new Set(consumed.map((c) => c.id));
    setSteerQueue(steerQueueRef.current.filter((q) => !consumedIds.has(q.id)));
    const snap = chatStore.getSnapshot().sessions.find((s) => s.id === session.id);
    if (!snap) return;
    const settled: ChatMessage[] = consumed.map((c) => ({
      id: c.id,
      role: "user",
      content: c.text,
      createdAt: Date.now(),
      status: "done",
    }));
    const msgs = [...snap.messages];
    let at = msgs.length;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === "assistant") {
        at = i;
        break;
      }
    }
    msgs.splice(at, 0, ...settled);
    chatStore.updateMessages(session.id, msgs);
  }

  // After a turn ends, reconcile any still-queued steers: those the agent folded
  // in settle into the thread; those still queued arrived after the last model
  // call (never seen) → re-send as a fresh turn so they aren't lost.
  async function reconcileSteer() {
    const queued = steerQueueRef.current;
    if (!session || !queued.length) return;
    let remaining: { id: string; text: string }[];
    try {
      remaining = (await api.pendingSteer(session.id)).pending;
    } catch {
      return; // can't tell consumed from not — leave the queue rather than guess
    }
    const remainingIds = new Set(remaining.map((r) => r.id));
    const consumed = queued.filter((q) => !remainingIds.has(q.id));
    const unconsumed = queued.filter((q) => remainingIds.has(q.id));
    if (consumed.length) settleConsumed(consumed);
    setSteerQueue([]);
    if (unconsumed.length) {
      void runTurn(unconsumed.map((u) => u.text).join("\n\n"));
    }
  }

  // Mid-turn ack: while a turn streams with queued steers, poll the backend so a
  // steer the agent has already folded in settles into the thread immediately —
  // otherwise a long turn shows "queued" long after the agent received it.
  useEffect(() => {
    if (status !== "streaming" || steerQueue.length === 0 || !session) return;
    let alive = true;
    const tick = async () => {
      try {
        const remaining = (await api.pendingSteer(session.id)).pending;
        if (!alive) return;
        const remainingIds = new Set(remaining.map((r) => r.id));
        const consumed = steerQueueRef.current.filter((q) => !remainingIds.has(q.id));
        if (consumed.length) settleConsumed(consumed);
      } catch {
        /* transient — retry next tick */
      }
    };
    const handle = window.setInterval(tick, 1500);
    return () => {
      alive = false;
      window.clearInterval(handle);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, steerQueue.length, session?.id]);

  function copyMessage(message: ChatMessage) {
    void navigator.clipboard?.writeText(message.content || "");
    setCopiedId(message.id ?? null);
    window.setTimeout(() => setCopiedId((id) => (id === message.id ? null : id)), 1500);
  }

  // Regenerate an assistant reply: drop it (and anything after) from the thread,
  // then re-run the user message that prompted it via the `hidden` path — no
  // duplicate user bubble, just a fresh streaming assistant. Only offered on the
  // last assistant message when idle.
  function regenerate(assistantId?: string) {
    if (!assistantId || !session || status === "streaming") return;
    const snap = chatStore.getSnapshot().sessions.find((s) => s.id === session.id);
    if (!snap) return;
    const i = snap.messages.findIndex((m) => m.id === assistantId);
    if (i < 0) return;
    const user = [...snap.messages.slice(0, i)].reverse().find((m) => m.role === "user");
    if (!user) return;
    chatStore.updateMessages(session.id, snap.messages.slice(0, i));
    void runTurn(user.content, { hidden: true });
  }

  // Fork the conversation at a message: open a NEW tab/session seeded with the
  // history up to and including that message, leaving the original untouched.
  // Continue the branch from there.
  function forkAtMessage(message: ChatMessage) {
    if (!session) return;
    const i = session.messages.findIndex((m) => m.id === message.id);
    if (i < 0) return;
    const seed = session.messages.slice(0, i + 1).map((m) => ({
      ...m,
      // a forked-from message is settled history in the new branch
      status: m.status === "streaming" ? "done" : m.status,
    }));
    const created = chatStore.createSession(); // becomes the current + active tab
    chatStore.updateMessages(created.id, seed);
    const baseTitle = session.title && session.title !== "New chat" ? session.title : "Chat";
    chatStore.renameSession(created.id, `${baseTitle} (fork)`);
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
    // Build off the live store snapshot, not the render-closure `messages` — a
    // regenerate trims the thread in the store then calls runTurn in the same tick
    // (before a re-render), so the closure copy would be stale.
    const base =
      chatStore.getSnapshot().sessions.find((s) => s.id === session.id)?.messages ?? messages;
    // `hidden` (an approval resume, or a regenerate) sends `content` to the server but
    // omits the user bubble — the agent still receives it, the chat just doesn't show it.
    chatStore.updateMessages(
      session.id,
      opts.hidden ? [...base, assistant] : [...base, userMessage, assistant],
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
        onSkills: (skills) => {
          // Skills the agent auto-retrieved for the turn — show as a chip above the
          // answer. Union by name across the turn's model calls (a later call may
          // pull a different set); keep each skill's description for the hover.
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) => {
              if (message.id !== assistantId) return message;
              const byName = new Map((message.skillsLoaded || []).map((s) => [s.name, s]));
              for (const s of skills) byName.set(s.name, s);
              return { ...message, skillsLoaded: [...byName.values()] };
            }),
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
      }, { images: opts.images, model: session.model, reasoningEffort: effectiveReasoningEffort(session) });
      chatStore.setSessionStatus(session.id, "idle");
      setStatusMessage("idle");
      void reconcileSteer();
    } catch (exc) {
      if (controller.signal.aborted) {
        setStatusMessage("stopped");
      } else {
        const message = errMsg(exc);
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
    // Drop any optimistic queued-steer bubbles; the user chose to stop.
    setSteerQueue([]);
  }

  if (!session) return null;

  return (
    <div className="chat-session-slot" hidden={!visible}>
      <Conversation>
        {messages.length === 0 ? (
          <Empty icon={<TerminalSquare />} description="No messages in this session." />
        ) : (
          messages.map((message) => (
            <Message
              key={message.id || `${message.role}-${message.createdAt}`}
              role={message.role}
              streaming={message.status === "streaming"}
            >
              {message.skillsLoaded && message.skillsLoaded.length > 0 ? (
                // Skills the agent auto-retrieved for this turn — a compact chip so the
                // user can see what guidance shaped the answer. Hover a name for its
                // description (the SKILL.md `description` / trigger signal).
                <div className="chat-skills-chip" role="note">
                  <BookOpen size={13} aria-hidden />
                  <span className="chat-skills-chip-label">Skills:</span>
                  {message.skillsLoaded.map((s, i) => (
                    <span key={s.name} className="chat-skills-chip-item" title={s.description || s.name}>
                      {s.name}
                      {i < message.skillsLoaded!.length - 1 ? <span aria-hidden> · </span> : null}
                    </span>
                  ))}
                </div>
              ) : null}
              {message.reasoning ? (
                // Collapsible "thinking" — open while the model is still reasoning
                // (no answer text yet), auto-collapses once the answer starts.
                <Reasoning surface="subtle" streaming={message.status === "streaming" && !message.content}>
                  {message.reasoning}
                </Reasoning>
              ) : null}
              {message.toolCalls && message.toolCalls.length > 0 ? (
                <ToolCalls calls={message.toolCalls} onCancelDelegation={cancelDelegation} />
              ) : null}
              {message.content
                ? message.role === "user"
                  ? // Literal user input — preserve newlines (markdown reflows assistant text).
                    <span className="chat-user-text">{message.content}</span>
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
              {message.role === "assistant" && message.status !== "streaming" && message.content ? (
                <MessageActions>
                  <MessageAction
                    label={copiedId === message.id ? "Copied" : "Copy"}
                    icon={copiedId === message.id ? <Check size={14} /> : <Copy size={14} />}
                    onClick={() => copyMessage(message)}
                  />
                  <MessageAction
                    label="Fork from here"
                    icon={<GitBranch size={14} />}
                    onClick={() => forkAtMessage(message)}
                  />
                  {message.id === lastAssistantId ? (
                    <MessageAction
                      label="Regenerate"
                      icon={<RotateCcw size={14} />}
                      disabled={status === "streaming"}
                      onClick={() => regenerate(message.id)}
                    />
                  ) : null}
                </MessageActions>
              ) : null}
            </Message>
          ))
        )}
        {steerQueue.map((q) => (
          // DS queued state (0.42.0): dimmed pending bubble + spinner + ✕. The ✕
          // hits DELETE /steer/{id}: if still queued it's dropped before the agent
          // sees it; if already folded in, cancelSteer settles it into the thread
          // (no lie that it never ran).
          <Message
            key={q.id}
            role="user"
            queued
            queuedLabel="queued — folds into the agent's work at its next step"
            onCancel={() => void cancelSteer(q.id)}
          >
            <span className="chat-user-text">{q.text}</span>
          </Message>
        ))}
      </Conversation>

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
          const files = filesFromTransfer(e.dataTransfer);
          if (files.length) {
            e.preventDefault();
            files.forEach((f) => void uploadAttachment(f));
          }
        }}
      >
        {status === "streaming" ? (
          // Live turn status. Stop now lives inside the composer (the DS busy mode's
          // self-contained onStop), so this strip is just the "working…" readout.
          <div className="composer-status">
            <Loader2 className="spin" size={12} />
            <span>{statusMessage || "working"}</span>
          </div>
        ) : null}
        <PromptInput
          value={draft}
          onChange={(v) => {
            setDraft(v);
            setSlashDismissed(false); // re-open the menu when the input changes
          }}
          // Idle → send. While a turn streams (`busy`), the field stays live: Enter
          // queues a steer into the running turn (onQueue) without stopping it, and
          // the kit renders a dedicated Stop (onStop) beside Send.
          onSubmit={() => void send()}
          busy={status === "streaming"}
          onQueue={() => void queueSteer()}
          onStop={() => void stop()}
          placeholder={
            status === "streaming"
              ? "Steer the agent — your message folds into its work at the next step (Enter to queue)"
              : "Message protoAgent  (/ for commands · Enter to send · ⌘/Ctrl+Enter for newline)"
          }
          inputRef={textareaRef}
          onKeyDown={onComposerKeyDown}
          onPaste={(e) => {
            // Paste-to-attach (the DS onPaste seam). Clipboard files — incl.
            // IMAGES/screenshots that some browsers expose only via items[] —
            // become attachments.
            const files = filesFromTransfer(e.clipboardData);
            if (files.length) {
              e.preventDefault();
              files.forEach((f) => void uploadAttachment(f));
              return;
            }
            // A large text paste becomes a removable attachment pill (routed
            // through the attach pipeline → tiered inline/indexed) instead of
            // flooding the field; small pastes fall through to the textarea.
            const text = e.clipboardData?.getData("text/plain") ?? "";
            if (isLargePaste(text)) {
              e.preventDefault();
              void uploadAttachment(pastedTextFile(text));
            }
          }}
          onAttach={() => fileInputRef.current?.click()}
          // The model picker lives in the DS composer's actions slot (ADR 0048 / the
          // ComposerWithAttachments DS pattern) — replaces the separate chip below.
          actions={<ComposerModelSelect />}
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
      </div>
    </div>
  );
}

