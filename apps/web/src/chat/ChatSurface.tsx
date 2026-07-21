import "./chat.css";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Spinner } from "@protolabsai/ui/data";
import { Switch } from "@protolabsai/ui/forms";
import { Conversation, Message, PromptInput } from "@protolabsai/ui/ai";
import { TabBar } from "@protolabsai/ui/navigation";
import { Check, EyeOff, TerminalSquare } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from "react";
import { useQuery } from "@tanstack/react-query";

import { openContextMenu } from "../contextMenu";
import { useIsMobile } from "../lib/useIsMobile";
import { useKbIntents } from "../keybindings/intents";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { goalsQuery, runtimeStatusQuery } from "../lib/queries";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import type { ChatMessage, ChatPart, HitlPayload, SlashCommand, SystemNoteTone, ToolCall } from "../lib/types";
import { HitlForm } from "./HitlForm";
import { notifyIfHidden } from "../lib/notify";
import {
  chatStore,
  useChatState,
  effectiveReasoningEffort,
  subscribeGoalKickoff,
  takeGoalKickoff,
} from "./chat-store";
import "./coreSlashCommands"; // registers /new, /clear, /effort via the slash-command seam (ADR 0061)
import { findSlashCommand, registeredSlashCommands, slashTokenAt } from "../ext/slashRegistry";
import type { ComposerFormSpec } from "../ext/slashRegistry";
import { useFlagPredicate } from "../flags/flags";
import { registeredComposerActions } from "../ext/composerRegistry";
import { ChatMessageView } from "./ChatMessageView";
import { ComposerModelSelect } from "./ComposerModelSelect";
import { useServerTurn, useServerTurnSessions } from "./server-turn-store";
import { filesFromTransfer, isLargePaste, pastedTextFile } from "./paste";
import { inputHistory, pushInputHistory } from "./inputHistory";
import { finalizeStoppedMessages, resolveStopTarget } from "./stopTurn";
import { addComponent, addToolRef, appendReasoning, appendText, replaceText } from "./parts";
import { createStreamWatchdog } from "./streamWatchdog";
import { ADD_SELECTOR, isIncognitoAddClick, trackShiftHeld } from "./shiftCue";
import { sessionsToClose } from "./bulkClose";

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
  const mobile = useIsMobile(); // hides the tab strip — MobileShell's SessionSheet replaces it
  // Sessions with a server-initiated turn in flight (push-resume / scheduled / watch) — those
  // turns don't touch sessionStatusMap, so without this their tab would read idle. Read once
  // here (the tab bar can't call the per-session hook inside its .map).
  const serverTurnSessions = useServerTurnSessions();
  const currentSession = chat.sessions.find((session) => session.id === chat.currentSessionId) || null;
  const [pendingClose, setPendingClose] = useState<string | null>(null);
  // Bulk close (others/left/right): GOAL tabs still waiting for their Stop/Detach confirm AFTER
  // the one in `pendingClose`. Only goal tabs are queued — plain tabs close inline in
  // startBulkClose — so we never parade a "Delete this chat?" dialog past each tab (the
  // dialog-storm the spec warns against), and exactly one dialog is ever open.
  const [closeQueue, setCloseQueue] = useState<string[]>([]);
  const [harvestOnDelete, setHarvestOnDelete] = useState(false);
  // Goal tab close: default keeps the goal running (detach); toggle on to STOP it (clear the
  // goal + close its task backlog) instead.
  const [stopGoalOnClose, setStopGoalOnClose] = useState(false);
  const pendingCloseSession = chat.sessions.find((s) => s.id === pendingClose) || null;
  // Active goals keyed by session — a tab whose session is driving a goal gets a different
  // close flow (detach + keep running) instead of the plain delete. Cached; refetches on
  // focus. `status: "active"` is the only in-flight state.
  const goalSessions = useQuery(goalsQuery()).data?.goals ?? [];
  const closingGoal = pendingClose
    ? goalSessions.find((g) => g.session_id === pendingClose && g.status === "active")
    : undefined;

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

  // Kick off a bulk close (Close others/left/right). `ids` is the already-resolved target list
  // (sessionsToClose, anchor excluded). Split it: plain tabs close immediately (no harvest —
  // matching the delete dialog's default); goal-driving tabs, whose Stop-vs-Detach choice can't
  // be defaulted safely, are queued through the SAME single-tab confirm one at a time.
  function startBulkClose(ids: string[]) {
    if (ids.length === 0) return;
    const activeGoalIds = new Set(
      goalSessions.filter((g) => g.status === "active").map((g) => g.session_id),
    );
    const goals = ids.filter((id) => activeGoalIds.has(id));
    for (const id of ids) {
      if (!activeGoalIds.has(id)) closeSession(id, false);
    }
    setHarvestOnDelete(false);
    setStopGoalOnClose(false);
    setPendingClose(goals[0] ?? null);
    setCloseQueue(goals.slice(1));
  }

  // A close dialog resolved (confirmed): promote the next queued goal tab into the dialog, or
  // close it when the queue is drained. Per-dialog toggles reset each step so every tab starts
  // from the default (harvest off, goal detach). For a single (non-bulk) close the queue is
  // empty, so this just clears the dialog.
  function advanceClose() {
    setHarvestOnDelete(false);
    setStopGoalOnClose(false);
    setPendingClose(closeQueue[0] ?? null);
    setCloseQueue((queue) => queue.slice(1));
  }

  // Cancel: abort the WHOLE bulk operation, not just the current tab — hitting cancel means
  // "stop closing", so the remaining queued tabs are spared.
  function cancelClose() {
    setPendingClose(null);
    setCloseQueue([]);
    setHarvestOnDelete(false);
    setStopGoalOnClose(false);
  }

  // Tab-strip Shift cues. While Shift is held the DS TabBar signals both Shift+click gestures:
  // the "+" becomes the incognito EyeOff (Shift+click → new incognito chat, #1697/#1744) and the
  // hovered ✕ becomes a red trashcan (Shift+click → quick-delete, no confirm/harvest, #1373).
  // One "is Shift held" signal drives both, via the `--incognito`/`--del` wrapper classes → CSS.
  const [shiftHeld, setShiftHeld] = useState(false);
  useEffect(() => trackShiftHeld(setShiftHeld), []);
  // The DS TabBar's onClose always opens the confirm dialog, so intercept the close-button
  // click in the CAPTURE phase (before the DS button's own onClick) when Shift is down and
  // delete directly. Maps the clicked ✕ to its session by sibling index (DOM = sessions order).
  function onTabBarClickCapture(e: ReactMouseEvent) {
    if (!e.shiftKey) return;
    // Shift+click the add "+" → new INCOGNITO session (#1697): the click-path twin of the
    // tab context menu's "New incognito chat" (same createSession({incognito:true})
    // semantics). Intercepted in the capture phase so the DS button's own onClick (the
    // plain add) never fires; a plain click is untouched.
    if (isIncognitoAddClick(e.target, e.shiftKey)) {
      e.preventDefault();
      e.stopPropagation();
      chatStore.createSession({ incognito: true });
      return;
    }
    const closeBtn = (e.target as HTMLElement).closest(".pl-tabbar__close");
    if (!closeBtn) return;
    const tabEl = closeBtn.closest(".pl-tabbar__tab") as HTMLElement | null;
    if (!tabEl) return;
    const tabs = Array.from((e.currentTarget as HTMLElement).querySelectorAll(".pl-tabbar__tab"));
    const session = chat.sessions[tabs.indexOf(tabEl)];
    if (!session) return;
    e.preventDefault();
    e.stopPropagation(); // beat the DS close button's onClick → no confirm dialog
    closeSession(session.id, false); // false = no knowledge harvest
  }

  // Keyboard twin of the Shift+click incognito gesture (#1697): Shift+Enter/Space on the
  // focused "+" also creates an incognito session. Keyboard activation synthesizes the
  // button's click AFTER keydown (Enter) / on keyup (Space), and its modifier state isn't
  // reliable across browsers — so intercept at keydown-capture and preventDefault, which
  // stops the synthetic click (and thus the DS onAdd) from ever firing.
  function onTabBarKeyDownCapture(e: ReactKeyboardEvent) {
    if (!e.shiftKey || (e.key !== "Enter" && e.key !== " ")) return;
    if (!(e.target as HTMLElement).closest(ADD_SELECTOR)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.repeat) return; // held key auto-repeats keydown — only the first press creates a session
    chatStore.createSession({ incognito: true });
  }

  // Right-click a chat tab → context menu (ADR 0036). The DS TabBar's `onTabContextMenu`
  // (@protolabsai/ui@0.53.0) hands us the session id directly — no sibling-index DOM sniffing.
  // Rename opens the TabBar's inline editor via a synthetic dblclick on the tab element (the DS
  // exposes no start-rename API); we grab that element off the event, not to recover WHICH tab
  // (the hook gives us the id) but only to fire the editor.
  function onTabContextMenu(id: string, e: ReactMouseEvent) {
    const tabEl = (e.target as HTMLElement).closest(".pl-tabbar__tab") as HTMLElement | null;
    const target = chat.sessions.find((s) => s.id === id);
    // Resolve each bulk-close target set up front (index math, anchor excluded). An empty set
    // means the entry is meaningless for this tab (e.g. "Close left" on the leftmost tab), so
    // the closure is passed only when it has something to close — the menu hides the rest.
    const others = sessionsToClose(chat.sessions, id, "others");
    const left = sessionsToClose(chat.sessions, id, "left");
    const right = sessionsToClose(chat.sessions, id, "right");
    openContextMenu("chat-tab", e, {
      sessionId: id,
      incognito: !!target?.incognito,
      onNew: () => chatStore.createSession(),
      onNewIncognito: () => chatStore.createSession({ incognito: true }),
      onToggleIncognito: () => chatStore.setSessionIncognito(id, !target?.incognito),
      onRename: () => tabEl?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true })),
      onClose: () => setPendingClose(id),
      onCloseOthers: others.length ? () => startBulkClose(others) : undefined,
      onCloseLeft: left.length ? () => startBulkClose(left) : undefined,
      onCloseRight: right.length ? () => startBulkClose(right) : undefined,
    });
  }

  // Right-click EMPTY tab-bar space (not a tab) → just the "New chat" affordance. onTabContextMenu
  // owns per-tab; this catches the background only, and bails on tab hits so the two never both fire.
  function onTabBarBackgroundContextMenu(e: ReactMouseEvent) {
    if ((e.target as HTMLElement).closest(".pl-tabbar__tab")) return; // a tab — onTabContextMenu owns it
    openContextMenu("chat-tab", e, {
      onNew: () => chatStore.createSession(),
      onNewIncognito: () => chatStore.createSession({ incognito: true }),
    });
  }

  return (
    <section className="panel stage-panel chat-stage" style={active ? undefined : { display: "none" }} aria-hidden={!active} data-kb-scope="chat">
      {/* DS TabBar (#832): a tab per session (status dot · title · close) + "+".
          Double-click a title to rename (TabBar owns the inline EditableText).
          `responsive` collapses to a DS-native <select> + add in a narrow panel
          (container query). The status dot rides the `icon` slot — wide-strip only:
          the collapsed <option> can't host markup, matching the old behavior. */}
      {/* Suppressed on phones: the `responsive` collapse is a <select>, a desktop idiom that
          reads as a form control rather than a thread switcher. The chat-first shell puts the
          session title in its header and opens SessionSheet on tap instead (MobileShell). */}
      {mobile ? null : (
      <div
        className={`chat-tabbar-wrap${shiftHeld ? " chat-tabbar-wrap--del chat-tabbar-wrap--incognito" : ""}`}
        onContextMenu={onTabBarBackgroundContextMenu}
        onClickCapture={onTabBarClickCapture}
        onKeyDownCapture={onTabBarKeyDownCapture}
      >
        <TabBar
          ariaLabel="Chat sessions"
          responsive
          activeId={chat.currentSessionId ?? ""}
          items={chat.sessions.map((session) => {
            const fg = chat.sessionStatusMap[session.id] || "idle";
            // Foreground streaming already lights the dot; also surface a background
            // server-turn as a pulsing "processing" dot so an unfocused tab doing work
            // doesn't read idle. error > streaming > processing > idle.
            const status =
              fg === "error"
                ? "error"
                : fg === "streaming"
                  ? "streaming"
                  : serverTurnSessions.has(session.id)
                    ? "processing"
                    : "idle";
            return {
              id: session.id,
              label: session.title,
              // Incognito rides the icon slot next to the status dot — the tab-level
              // "this thread leaves no memory" indicator (ADR 0069 D3b).
              icon: session.incognito ? (
                <span className="session-tab-icons">
                  <span className={`session-dot ${status}`} title={status} />
                  <EyeOff size={12} className="session-incognito-icon" aria-label="incognito" />
                </span>
              ) : (
                <span className={`session-dot ${status}`} title={status} />
              ),
            };
          })}
          onSelect={(id) => chatStore.switchSession(id)}
          onClose={(id) => setPendingClose(id)}
          onRename={(id, label) => chatStore.renameSession(id, label)}
          onReorder={(next) => chatStore.reorderSessions(next.map((t) => t.id))}
          onAdd={() => chatStore.createSession()}
          onTabContextMenu={onTabContextMenu}
          // The DS TabBar renders this as the + button's native title/aria-label — the
          // hover hint for the Shift+click incognito gesture (#1697). Shift+Enter is the
          // keyboard twin (onTabBarKeyDownCapture), so the label teaches both paths.
          addLabel="New chat — Shift+click for incognito (Shift+Enter when focused)"
          // NOT wired to ui@0.58's `addDisabled`, deliberately. The store reuses a pristine
          // blank rather than duplicating it, so a plain click on an already-blank tab is a
          // no-op — but this "+" is a DUAL-gesture control (Shift+click / Shift+Enter opens
          // an INCOGNITO chat, #1697), and a disabled button fires no events, so dimming it
          // would kill that gesture too. Disabling only when BOTH creates are no-ops is
          // correct but fires so rarely it doesn't fix the wart. The store guard already
          // prevents the pile-up; a live-but-inert plain click is the residue.
          // MobileShell/SessionSheet have no such conflict and DO disable their buttons.
        />
      </div>
      )}

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
        title={closingGoal ? "Close this goal tab?" : "Delete this chat?"}
        confirmLabel={closingGoal ? (stopGoalOnClose ? "Stop goal & close" : "Keep running, close tab") : "Delete chat"}
        destructive={!closingGoal || stopGoalOnClose}
        onConfirm={() => {
          if (pendingClose) {
            if (closingGoal) {
              if (stopGoalOnClose) {
                // STOP: clear the goal + close its task backlog, and purge the (now finished)
                // session. The tab unmount aborts the in-flight drive stream.
                void api.clearGoal(pendingClose, true).catch(() => {});
                void api.deleteChatSession(pendingClose, false).catch(() => {});
                chatStore.deleteSession(pendingClose);
              } else {
                // DETACH: keep the goal driving in the background (a headless continuation) and
                // KEEP the server session — its checkpoint is the goal's accumulated context, so
                // we must NOT purge it. Just drop the tab locally; track it in the Goals panel.
                void api.resumeGoal(pendingClose).catch(() => {});
                chatStore.deleteSession(pendingClose);
              }
            } else {
              closeSession(pendingClose, harvestOnDelete);
            }
          }
          // Advance the bulk-close queue (or just clear the dialog when it's a single close).
          advanceClose();
        }}
        onClose={cancelClose}
      >
        {pendingCloseSession ? (
          closingGoal ? (
            <>
              <p style={{ margin: 0 }}>
                This tab is driving the goal <strong>{`"${closingGoal.condition || pendingCloseSession.title}"`}</strong>.
                By default it keeps running in the background — track it in the Goals panel.
              </p>
              {/* Opt-in STOP: cancel the goal AND close the tasks it filed (its backlog). */}
              <Switch
                className="chat-delete-harvest"
                checked={stopGoalOnClose}
                onCheckedChange={setStopGoalOnClose}
                label="Stop the goal and close its open tasks instead"
              />
            </>
          ) : (
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
          )
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
  // Turn status is still tracked (drives the stream lifecycle) but no longer surfaced as
  // a spinner/"working…" strip above the composer — the inline indicators cover it now.
  const [, setStatusMessage] = useState("");
  const [taskId, setTaskId] = useState("");
  const [hitl, setHitlState] = useState<HitlPayload | null>(null);
  // Ref mirror for async closures (reconcileSteer runs after an await — the render
  // closure's `hitl` is stale by then). Always set both via updateHitl.
  const hitlRef = useRef<HitlPayload | null>(null);
  const updateHitl = (payload: HitlPayload | null) => {
    hitlRef.current = payload;
    setHitlState(payload);
  };
  const abortRef = useRef<AbortController | null>(null);
  // Auto-drive a goal created from the Work panel: that flow opens this tab (`kick:false`)
  // and, once the goal is set on the server, registers a kickoff on the chat-store seam. Fire
  // it as a HIDDEN turn so the drive loop streams live INTO this tab (the server's iteration-0
  // kickoff injection re-states the goal). `check()` also covers a kickoff registered before
  // this slot mounted; `takeGoalKickoff` is idempotent, so it fires exactly once.
  useEffect(() => {
    const check = () => {
      const kickoff = takeGoalKickoff(sessionId);
      if (kickoff) void runTurn(kickoff, { hidden: true });
    };
    check();
    return subscribeGoalKickoff(check);
    // runTurn is a stable per-render closure that reads the live store; sessionId is the key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);
  // Client composer-form (#1701): a form a CLIENT command opens in the composer (e.g.
  // `/effort`'s picker), rendered through the same HitlForm but resolved LOCALLY — no
  // agent round-trip. Kept DISTINCT from the agent `hitl` interrupt so the two never
  // collide; the agent interrupt takes precedence when both somehow exist.
  const [composerForm, setComposerForm] = useState<ComposerFormSpec | null>(null);
  // Transient "copied ✓" feedback on a message's copy action.
  const [copiedId, setCopiedId] = useState<string | null>(null);
  // The message a "Rewind to here" is pending confirmation on (null = dialog closed).
  // Rewind is destructive (discards everything below), so it goes through a confirm.
  const [pendingRewind, setPendingRewind] = useState<ChatMessage | null>(null);
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
  // Terminal-style ↑/↓ history nav (#1496): position in the shared submitted-message ring
  // (null = not navigating), and the live draft stashed when nav began (restored on ↓ past
  // the newest). Refs, not state — they change alongside a setDraft, no separate re-render.
  const histIndexRef = useRef<number | null>(null);
  const histStashRef = useRef<string>("");
  // Autofocus the composer when this becomes the active session AND the chat surface is
  // the active rail surface — so clicking the Chat rail item (or switching tabs) lands
  // focus in the composer without a click. (`visible` alone is the active tab, which
  // doesn't change when you switch INTO the chat surface from another rail item.)
  useEffect(() => {
    if (visible && surfaceActive) textareaRef.current?.focus();
  }, [visible, surfaceActive]);
  // The global "focus composer" keybinding (ADR 0063 — `/`) bumps this nonce; only the
  // VISIBLE + active slot grabs focus (others no-op), same gate as the autofocus above.
  const composerFocusNonce = useKbIntents((s) => s.composerFocusNonce);
  useEffect(() => {
    if (composerFocusNonce && visible && surfaceActive) textareaRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composerFocusNonce]);
  const status = chat.sessionStatusMap[sessionId] || "idle";
  // A server-initiated turn (background push-resume / scheduled / watch fire, #1767) is
  // running into THIS session — the browser can't stream it, so show a labelled typing
  // indicator. Suppressed while this tab is itself streaming (its own spinner covers it).
  const serverTurnLabel = useServerTurn(sessionId);

  // Pending file attachments. Each is uploaded to /api/knowledge/attach on pick;
  // the backend tiers it (inline small / index large) and returns a `context`
  // block we prepend to the SENT message (not the visible bubble) on send.
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  // Native vision: when the active model accepts images, attached images go
  // straight to the model as multimodal parts; otherwise they take the pipeline.
  const { data: runtime } = useQuery(runtimeStatusQuery());
  const visionModel = Boolean(runtime?.model?.vision);
  // A configured vision model can DESCRIBE images for a text-only chat model (#1381), so an
  // image attaches via the pipeline instead of erroring.
  const imageDescribe = Boolean(runtime?.model?.image_describe);

  async function uploadAttachment(file: File) {
    const id = messageId();
    const kind: "file" | "image" = file.type.startsWith("image/") ? "image" : "file";
    setAttachments((a) => [...a, { id, name: file.name, kind, status: "uploading" }]);

    // Images always ride the turn natively as multimodal parts: a vision model
    // sees them directly, and on a text-only model the server still bridges them
    // into the media store so image tools can act on them by id (#1969) — the
    // old hard error (#1374) is gone.
    if (kind === "image") {
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
        return;
      }
      // A configured describe model (#1381) still adds a textual description for a
      // text-only chat model — best-effort context alongside the native part; a
      // describe failure never sinks the already-ready attachment.
      if (visionModel || !imageDescribe) return;
      try {
        const form = new FormData();
        form.append("file", file);
        form.append("session_id", sessionId);
        const r = await api.attachToChat(form);
        if (r.enabled && r.context) {
          setAttachments((a) => a.map((x) => (x.id === id ? { ...x, context: r.context } : x)));
        }
      } catch {
        // native attachment already succeeded; description is additive
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
  // fetched once; the dropdown is active while typing a "/name" token (before a space).
  const [commands, setCommands] = useState<SlashCommand[]>([]);
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  // The "/name" token the caret currently sits in ({query, start}), or null. Recomputed
  // from the LIVE textarea caret (not just the draft) so the popover triggers MID-INPUT —
  // typing "/" at any cursor position opens it, not only when "/" is char 0 (#1530).
  const [slashCtx, setSlashCtx] = useState<{ query: string; start: number; end: number } | null>(null);
  // Keeps the keyboard-selected item scrolled into view during ↑/↓ nav (#1528).
  const activeSlashRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    api.chatCommands().then((r) => setCommands(r.commands)).catch(() => {});
  }, []);

  // Re-parse the slash token from the textarea's current value + caret. Called on input,
  // on caret moves (native keyup/click/select/focus listeners below), and after any
  // programmatic caret change — so the popover state tracks the caret wherever it is.
  const refreshSlash = useCallback(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    setSlashCtx(slashTokenAt(ta.value, ta.selectionStart ?? ta.value.length));
  }, []);

  // Caret moves that don't fire onChange (arrow keys, clicks, selection, focus) still need
  // to re-evaluate the popover so "/" mid-input opens/closes as the caret enters/leaves a token.
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.addEventListener("keyup", refreshSlash);
    ta.addEventListener("click", refreshSlash);
    ta.addEventListener("select", refreshSlash);
    ta.addEventListener("focus", refreshSlash);
    return () => {
      ta.removeEventListener("keyup", refreshSlash);
      ta.removeEventListener("click", refreshSlash);
      ta.removeEventListener("select", refreshSlash);
      ta.removeEventListener("focus", refreshSlash);
    };
  }, [refreshSlash]);

  const slashQuery = slashDismissed ? null : slashCtx?.query ?? null;

  // Developer-flag gate (ADR 0068): a registered command tagged with `flag:` is listed
  // and dispatched only while its flag resolves ON — flag-off, it's as if unregistered.
  const flagOn = useFlagPredicate();

  const slashMatches = useMemo(() => {
    if (slashQuery === null) return [];
    const q = slashQuery.toLowerCase();
    // Client-side commands (ADR 0061) surface first, then server skills. The client set
    // comes from the slash-command registry — core (/new, /clear, /effort) AND any fork-
    // registered commands — so neither is hardcoded here.
    const all: SlashCommand[] = [
      ...registeredSlashCommands()
        .filter((c) => !c.flag || flagOn(c.flag))
        .map((c) => ({ name: c.name, description: c.description, usage: c.usage })),
      ...commands,
    ];
    // Dedup by token: a command that exists BOTH as a client command and a server skill
    // (e.g. /goal, /clear) must appear once — the client entry (listed first) wins.
    const seen = new Set<string>();
    const unique = all.filter((c) => {
      const n = c.name.toLowerCase();
      if (seen.has(n)) return false;
      seen.add(n);
      return true;
    });
    return unique.filter(
      (c) => !q || c.name.toLowerCase().includes(q) || c.description.toLowerCase().includes(q),
    );
  }, [slashQuery, commands, flagOn]);

  const slashActive = slashMatches.length > 0;
  const slashSel = slashActive ? Math.min(slashIndex, slashMatches.length - 1) : 0;

  // Auto-scroll the keyboard-selected item into view during ↑/↓ nav so it never hides
  // below the popover's scroll edge (standard listbox behavior, #1528).
  useEffect(() => {
    if (slashActive) activeSlashRef.current?.scrollIntoView({ block: "nearest" });
  }, [slashSel, slashActive]);

  // Post a local SYSTEM NOTE to the thread (e.g. a /effort confirmation, a status line, a
  // warning) — never sent to the agent, just shown so the operator sees a local action took
  // effect. role "system" so it renders distinctly and never gets the answer action row
  // (copy/fork/regenerate). `tone` colours it (info/warning/danger/success). This is the reusable
  // seam for any non-agent in-thread notice — exposed to forks via the slash/composer registries.
  function noteToThread(text: string, opts?: { tone?: SystemNoteTone }) {
    if (!session) return;
    const base = chatStore.getSnapshot().sessions.find((s) => s.id === session.id)?.messages ?? [];
    chatStore.updateMessages(session.id, [
      ...base,
      { id: messageId(), role: "system", content: text, noteTone: opts?.tone, createdAt: Date.now(), status: "done" },
    ]);
  }

  // Dispatch a CLIENT-SIDE slash command through the registry (ADR 0061) — run locally,
  // never sent to the agent. A registered `/<verb>` CLAIMS the token (the frontend twin of
  // the backend's `register_chat_command`): we build the SlashContext from local state +
  // invoke its handler. `raw` is the command minus the slash, e.g. "effort high". Returns
  // true if a command handled it (caller clears the draft + skips the send); false ⇒ not a
  // client command (fall through to the server / draft path). Core commands (/new, /clear,
  // /effort) and any fork-registered commands flow through here identically.
  function runClientSlash(raw: string): boolean {
    const [verb, ...rest] = raw.split(/\s+/);
    const cmd = findSlashCommand(verb);
    if (!cmd) return false;
    if (cmd.flag && !flagOn(cmd.flag)) return false; // flag-off ⇒ as if unregistered
    return cmd.run({
      rest: rest.join(" ").trim(),
      sessionId: session?.id ?? null,
      noteToThread,
      setDraft,
      focusComposer: () => textareaRef.current?.focus(),
      // Open a form in the composer panel, resolved locally (#1701) — /effort's picker.
      openForm: setComposerForm,
      // Registry-enumerating commands (/help) see the HOST's visibility rules + the live
      // server command list — never a hardcoded copy of either.
      flagOn,
      serverCommands: commands,
    });
  }

  function completeCommand(cmd: SlashCommand) {
    // Replace ONLY the "/name" token the caret is in — surrounding text is preserved so a
    // command can be completed at the start, middle, or end of the draft (#1530). Fall back
    // to the whole draft if the token is somehow unknown (defensive).
    const token = slashCtx;
    const start = token ? token.start : 0;
    const end = token ? token.end : draft.length;
    // Place the caret + re-sync the popover after React commits the new value.
    const settleCaret = (pos: number) => {
      requestAnimationFrame(() => {
        const ta = textareaRef.current;
        if (!ta) return;
        // A form the command opened (e.g. /model's picker — possibly ASYNC, after a
        // schema fetch) owns focus on appear (#1978) — don't yank it back. Checked at
        // fire time against the DOM: state/refs can't see an openForm that hasn't
        // happened yet. Either race order converges on the form keeping focus.
        if (document.activeElement?.closest(".hitl-float")) return;
        ta.focus();
        ta.selectionStart = ta.selectionEnd = pos;
        refreshSlash();
      });
    };
    // A client command runs on pick — drop just its token from the draft (keeping any
    // surrounding text); a server skill inserts "/name " to edit + send.
    if (runClientSlash(cmd.name)) {
      setDraft(draft.slice(0, start) + draft.slice(end));
      setSlashIndex(0);
      setSlashDismissed(true);
      setSlashCtx(null);
      settleCaret(start);
      return;
    }
    const insert = `/${cmd.name} `;
    setDraft(draft.slice(0, start) + insert + draft.slice(end));
    setSlashIndex(0);
    setSlashDismissed(true); // a space follows, so it would close anyway
    setSlashCtx(null);
    settleCaret(start + insert.length);
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
    // Terminal-style input history (#1496): ↑ recalls the previous submitted message when the
    // caret is on the FIRST line; ↓ walks back toward the live draft when on the LAST line — so
    // multi-line editing keeps normal caret movement and history only triggers at the edges.
    // (Bare arrows only — a modifier means a tab-jump / caret combo, not history.)
    if (
      (event.key === "ArrowUp" || event.key === "ArrowDown") &&
      !event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey
    ) {
      const ta = textareaRef.current;
      const hist = inputHistory();
      if (ta && hist.length) {
        const caret = ta.selectionStart ?? 0;
        const onFirstLine = draft.slice(0, caret).indexOf("\n") === -1;
        const onLastLine = draft.slice(ta.selectionEnd ?? caret).indexOf("\n") === -1;
        const recall = (val: string) => {
          setDraft(val);
          // caret to end so the next keystroke edits the recalled text (readline behaviour)
          requestAnimationFrame(() => {
            const t = textareaRef.current;
            if (t) {
              t.selectionStart = t.selectionEnd = val.length;
              refreshSlash(); // keep the slash popover in sync with the moved caret
            }
          });
        };
        if (event.key === "ArrowUp" && onFirstLine) {
          event.preventDefault();
          if (histIndexRef.current === null) {
            histStashRef.current = draft; // remember the in-progress draft
            histIndexRef.current = hist.length - 1;
          } else if (histIndexRef.current > 0) {
            histIndexRef.current -= 1;
          }
          recall(hist[histIndexRef.current]);
          return;
        }
        if (event.key === "ArrowDown" && histIndexRef.current !== null && onLastLine) {
          event.preventDefault();
          histIndexRef.current += 1;
          if (histIndexRef.current > hist.length - 1) {
            histIndexRef.current = null; // walked past the newest → restore the stashed draft
            recall(histStashRef.current);
            histStashRef.current = "";
          } else {
            recall(hist[histIndexRef.current]);
          }
          return;
        }
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
          refreshSlash(); // keep the slash popover in sync with the moved caret
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
    // A HITL form/question/approval is open (#1560): a fresh send would race the
    // pending form — the server holds unmarked messages anyway — so queue it as a
    // steer instead. It folds into the agent's context right AFTER the form
    // response (submit or dismiss), with the same queued-bubble + ✕ affordances
    // as mid-turn steering. Attachments stay in the tray for the next real send.
    if (hitl) {
      void queueSteer();
      return;
    }
    const text = draft.trim();
    pushInputHistory(text); // record for ↑/↓ recall, then reset nav to the newest
    histIndexRef.current = null;
    histStashRef.current = "";
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
    // Set-dedupe: a text-only-model image with a describe model is BOTH piped
    // (context) and native (#1969) — list its name once.
    const names = [...new Set([...piped, ...nativeImgs].map((a) => a.name))].join(", ");
    const display = text ? `${text}\n\nAttached: ${names}` : `Attached: ${names}`;
    setAttachments([]);
    void runTurn(display, { sendAs: sent, images });
  }

  // Steer a RUNNING turn: queue the typed message (folds in at the agent's next
  // model call via SteeringMiddleware) without stopping the stream. Shows an
  // optimistic "queued" bubble; turn-end reconcile settles or re-sends it.
  async function queueSteer() {
    const text = draft.trim();
    if (!session || !text) return;
    pushInputHistory(text); // steered messages join the same recall ring
    histIndexRef.current = null;
    histStashRef.current = "";
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
    // The turn parked on a HITL form (#1560): steers the agent hasn't folded yet stay
    // QUEUED — the server keeps holding them and folds them in right after the form
    // response. Re-sending them as a fresh turn here would deliver them BEFORE the
    // form answer (and abandon the pending interrupt).
    if (unconsumed.length && hitlRef.current) {
      setSteerQueue(unconsumed);
      return;
    }
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
  // Drop a local-only errored turn from the transcript (#1695). A hard turn error
  // parks the assistant bubble at status "error" with the error as content — it's
  // never backend history (a reload omits it), so removing it here is purely local
  // and safe. Only errored messages expose the Dismiss action that calls this.
  function dismissErroredMessage(messageId: string) {
    if (!session) return;
    const snap = chatStore.getSnapshot().sessions.find((s) => s.id === session.id);
    if (!snap) return;
    chatStore.updateMessages(
      session.id,
      snap.messages.filter((m) => m.id !== messageId),
    );
    // Clear the session's error dot too (it drove the red status pill).
    if (status === "error") chatStore.setSessionStatus(session.id, "idle");
  }

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

  // Rewind the conversation to a message IN PLACE (vs fork's new tab): discard
  // everything below it. Destructive + irreversible, so it's gated behind a confirm
  // (pendingRewind opens the dialog); confirmRewind does the work. The server rewrite
  // is the point — the LangGraph checkpoint is the agent's real context, so a
  // client-only trim would leave the agent still "remembering" the discarded turns.
  function rewindAtMessage(message: ChatMessage) {
    if (!session || status === "streaming") return;
    setPendingRewind(message);
  }

  async function confirmRewind(message: ChatMessage) {
    if (!session) return;
    const i = session.messages.findIndex((m) => m.id === message.id);
    if (i < 0) return;
    // WHICH occurrence of this exact text the clicked bubble is — client message ids never
    // appear in the checkpoint, so the server resolves by content; identical replies can
    // repeat, and this makes it pick the SAME one we clicked (not a later duplicate).
    const want = (message.content || "").trim();
    const occurrence = session.messages.slice(0, i).filter((m) => (m.content || "").trim() === want).length;
    let found: boolean;
    try {
      // Roll the agent's live context back on the server FIRST (the checkpoint is
      // the real memory); the client truncate below just mirrors the result.
      found = (await api.rewindChatSession(session.id, message.id ?? "", message.content, occurrence)).found;
    } catch (e) {
      onError(`Couldn't rewind: ${errMsg(e)}`);
      return;
    }
    // The server couldn't locate the message in the live checkpoint — leave the
    // client thread intact rather than diverge (the agent would still "remember"
    // turns the UI had dropped).
    if (!found) {
      onError("Couldn't rewind — that message is no longer in the agent's live context.");
      return;
    }
    // Keep the prefix through the selected message; drop everything after it.
    const snap = chatStore.getSnapshot().sessions.find((s) => s.id === session.id);
    const base = snap?.messages ?? session.messages;
    const at = base.findIndex((m) => m.id === message.id);
    chatStore.updateMessages(session.id, base.slice(0, (at < 0 ? i : at) + 1));
  }

  // Resume a paused (input-required) turn: submitting the HITL form/question
  // sends the response as a follow-up on the same session — the server feeds it
  // to the agent via Command(resume=…). A form response is serialized to JSON.
  // Redeem a plugin composer-form (#1701 Slice 2): POST the field values to the plugin's
  // on_submit. A reply becomes a note; a returned form is the next step of a wizard
  // (re-opened on the same input-required HITL path).
  async function submitPluginForm(callbackId: string, answers: Record<string, unknown>) {
    try {
      const res = await api.submitChatCommandForm({
        callback_id: callbackId,
        session_id: session?.id ?? "",
        answers,
      });
      if (res?.form) {
        updateHitl({ ...res.form, plugin_callback_id: res.callback_id });
      } else if (res?.reply) {
        noteToThread(String(res.reply));
      }
    } catch (e) {
      noteToThread(`⚠️ ${e instanceof Error ? e.message : String(e)}`, { tone: "danger" });
    }
  }

  async function resumeHitl(response: Record<string, unknown> | string) {
    // A plugin composer-form (#1701 Slice 2) rode the input_required frame but is NOT a
    // graph interrupt — redeem it via the plugin submit route, never Command(resume).
    if (hitl?.plugin_callback_id) {
      const cb = hitl.plugin_callback_id;
      updateHitl(null);
      await submitPluginForm(cb, typeof response === "string" ? {} : response);
      return;
    }
    // An approval gate (Approve/Deny on, e.g., run_command) isn't conversation — resume
    // the turn but DON'T append an "approved"/"denied" user bubble. The outcome lives on
    // the tool card itself (running → done on approve, error on deny), so the bubble is
    // just noise. A form/question answer IS meaningful content, so those stay visible.
    const silent = hitl?.kind === "approval";
    updateHitl(null);
    // For an approval resume, CONTINUE the original assistant message (the one that paused) so the
    // pre- and post-approval tool cards live in ONE bubble / one WorkBlock — otherwise they split
    // across two message bubbles with a gap between them. Forms/questions keep the new-bubble path
    // (their answer is meaningful conversation).
    // `hitlResume` marks this as THE answer to the pending interrupt (#1560): the server
    // resumes the parked graph with it, while any other message sent meanwhile is held
    // and folds in right after.
    void runTurn(
      typeof response === "string" ? response : JSON.stringify(response),
      silent ? { hidden: true, resumeMessageId: lastAssistantId, hitlResume: true } : { hitlResume: true },
    );
  }

  // Dismiss a paused (input-required) form/question WITHOUT answering it. Clearing the card
  // alone would leave the task parked in input-required forever — that state is exempt from
  // the server TTL sweep, so the LangGraph thread would never settle. Instead RESUME the turn
  // with an explicit "dismissed" sentinel so the agent continues and the task reaches a
  // terminal state. A dismissal isn't conversation content, so resume silently and continue
  // the paused assistant message (matching the approval-resume path) rather than minting a
  // new bubble.
  async function dismissHitl() {
    // A plugin composer-form has no parked graph to resume — just close it; its server
    // callback expires on its own TTL (#1701 Slice 2).
    if (hitl?.plugin_callback_id) {
      updateHitl(null);
      return;
    }
    updateHitl(null);
    void runTurn(
      "[dismissed] The operator dismissed this request without providing input. Continue " +
        "without it — proceed using your best judgment, or stop and explain what you need.",
      { hidden: true, resumeMessageId: lastAssistantId, hitlResume: true },
    );
  }

  async function runTurn(
    content: string,
    opts: {
      hidden?: boolean;
      sendAs?: string;
      images?: { b64: string; mime: string; name: string }[];
      resumeMessageId?: string;
      // This message answers the pending HITL interrupt (#1560) — see resumeHitl.
      hitlResume?: boolean;
    } = {},
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
    // On an approval resume, CONTINUE the original assistant message (`resumeMessageId`) instead of
    // minting a fresh bubble — so the pre- and post-approval tool cards extend ONE message / one
    // WorkBlock with no inter-bubble gap. Otherwise mint a new assistant message as usual.
    const resuming = opts.resumeMessageId != null;
    const assistantId = opts.resumeMessageId ?? messageId();
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
    // A resume flips the SAME assistant message back to streaming (keeping its parts/toolCalls).
    chatStore.updateMessages(
      session.id,
      resuming
        ? base.map((m) => (m.id === assistantId ? { ...m, status: "streaming" } : m))
        : opts.hidden
          ? [...base, assistant]
          : [...base, userMessage, assistant],
    );
    chatStore.setSessionStatus(session.id, "streaming");
    onError("");

    const controller = new AbortController();
    abortRef.current = controller;

    // Whether the stream delivered an AUTHORITATIVE full-turn text (a replace —
    // the terminal artifact's append:false canonical re-send, or a terminal task
    // frame). When the connection drops that frame (a proxy/tailnet blip at turn
    // end), the settled bubble is only the client's own delta accumulation — any
    // divergence (a lost or doubly-delivered chunk, #1938) would persist as
    // done-but-wrong with nothing left to correct it. Track it so the settle path
    // below can reconcile against the durable task exactly when it's needed.
    let sawAuthoritativeText = false;
    let turnTaskId = "";

    // Stalled-stream watchdog (hung-workblock fix). The chat's only "turn done"
    // signal is the SSE stream closing (onDone) — there is no standalone terminal
    // event. If the stream stalls open mid-turn — a large answer whose terminal
    // frames were stranded when the server's producer got cancelled on teardown
    // (a2a_impl/registry.py), or a proxy/tailnet buffer — the reader blocks
    // forever, so onDone, the post-stream reconcile below, and `finally` never
    // run, and the bubble spins "Working…" until reload. Guard it: after
    // WATCHDOG_IDLE_MS with no frames, consult the durable task (tasks/get). If
    // it's TERMINAL the server finished and the tail was lost → finalize from the
    // task and drop the dead socket; if it's still working the turn is just
    // legitimately quiet (a slow tool) → keep waiting.
    const WATCHDOG_IDLE_MS = 45_000;
    let settledByWatchdog = false;
    const finalizeFromTask = (state: string, text: string) => {
      const failed = /fail|cancel/i.test(state);
      const latest = chatStore.getSnapshot().sessions.find((s) => s.id === session.id);
      if (latest) {
        const now = Date.now();
        chatStore.updateMessages(
          session.id,
          latest.messages.map((m) => {
            if (m.id !== assistantId) return m;
            const toolCalls = m.toolCalls?.map((c) =>
              c.status === "running"
                ? { ...c, status: "done" as const, durationMs: c.durationMs ?? (c.startedAt !== undefined ? now - c.startedAt : undefined) }
                : c,
            );
            return {
              ...m,
              content: text || m.content,
              parts: text ? replaceText(m.parts, text, m.content) : m.parts,
              status: failed ? "error" : "done",
              toolCalls,
            };
          }),
        );
      }
      chatStore.setSessionStatus(session.id, failed ? "error" : "idle");
      setStatusMessage(failed ? "failed" : "idle");
    };
    const watchdog = createStreamWatchdog({
      idleMs: WATCHDOG_IDLE_MS,
      getTask: async () => {
        if (!turnTaskId) throw new Error("task id not surfaced yet");
        return api.getTask(turnTaskId);
      },
      onTerminal: (task) => {
        if (settledByWatchdog || controller.signal.aborted) return;
        settledByWatchdog = true;
        finalizeFromTask(task.state, task.text);
        controller.abort(); // release the stalled socket; unwinds via catch → finally
      },
    });
    const bumpWatchdog = () => {
      if (controller.signal.aborted) return;
      watchdog.bump();
    };
    const clearWatchdog = () => watchdog.stop();

    try {
      bumpWatchdog();
      await api.streamChat(sent, session.id, {
        signal: controller.signal,
        onTaskId: (id) => {
          turnTaskId = id;
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
        onStatus: (m) => {
          bumpWatchdog();
          setStatusMessage(m);
        },
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
          updateHitl(payload);
          // Alert natively if the window is hidden/unfocused (menu-bar-only
          // desktop, or a backgrounded tab) so the form isn't missed.
          notifyIfHidden(
            payload.title || "protoAgent needs your input",
            payload.question || payload.description,
          );
        },
        onText: (text, append) => {
          bumpWatchdog();
          if (!append) sawAuthoritativeText = true;
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) =>
              message.id === assistantId
                ? {
                    ...message,
                    content: append ? `${message.content}${text}` : text,
                    // A replace spans the WHOLE turn's text (the terminal frame re-sends
                    // the full canonical answer, preamble included) — replaceText keeps
                    // the streamed interleaving when nothing diverged and rebuilds
                    // otherwise; appendText's open-run rewrite would double a pre-tool
                    // preamble.
                    parts: append
                      ? appendText(message.parts, text, true)
                      : replaceText(message.parts, text, message.content),
                    status: "streaming",
                  }
                : message,
            ),
          );
        },
        onReasoning: (delta) => {
          bumpWatchdog();
          // Accumulate the streamed scratch_pad two ways: into `reasoning` (the
          // flat block kept for history/persistence) AND into the ordered `parts`,
          // so live turns render thinking inline at the point it occurred — between
          // the tool calls it precedes — rather than hoisted to the top.
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) =>
              message.id === assistantId
                ? {
                    ...message,
                    reasoning: `${message.reasoning ?? ""}${delta}`,
                    parts: appendReasoning(message.parts, delta),
                  }
                : message,
            ),
          );
        },
        onToolCall: (evt) => {
          bumpWatchdog();
          // `show_component` is a render directive, not a real action — its output IS the
          // inline component (delivered via onComponent / message.components). Suppress its
          // tool card so it doesn't add noise to the collapsed work timeline (#1323).
          if (evt.name === "show_component") return;
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) => {
              if (message.id !== assistantId) return message;
              const calls = [...(message.toolCalls || [])];
              const idx = calls.findIndex((c) => c.id === evt.id);
              const now = Date.now();
              // Ordered render blocks: a top-level tool opens/extends a tool group in
              // emission order; children (parentId set) nest under their parent's card,
              // so they don't get their own block.
              let nextParts = message.parts;
              if (evt.phase === "start") {
                // Nest a subagent's own tool under its `task` card. The server tags the
                // child frame with the parent delegation's id (authoritative — works even
                // though the task's end races AHEAD of the child); fall back to "last open
                // task wins" only for older servers that don't send it.
                const openTask = [...calls]
                  .reverse()
                  .find((c) => c.name === "task" && c.status === "running" && c.id !== evt.id);
                const card: ToolCall = {
                  id: evt.id,
                  name: evt.name,
                  input: evt.input,
                  status: "running",
                  startedAt: now,
                  parentId: evt.parentId ?? openTask?.id,
                };
                if (idx >= 0) calls[idx] = { ...calls[idx], ...card };
                else calls.push(card);
                if (card.parentId == null) nextParts = addToolRef(message.parts, evt.id);
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
                  // Missed start — treat as a fresh top-level call so it still renders.
                  calls.push({ id: evt.id, name: evt.name, output: evt.output, status: endStatus });
                  nextParts = addToolRef(message.parts, evt.id);
                }
              }
              return { ...message, toolCalls: calls, parts: nextParts };
            }),
          );
        },
        onComponent: (spec) => {
          // A renderable component (ADR 0051) — add it as an ORDERED part at its emission
          // point so it renders ABOVE the answer text that streams in after (#1323). `components`
          // is kept as the history/persistence fallback for messages without ordered parts.
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) =>
              message.id === assistantId
                ? {
                    ...message,
                    parts: addComponent(message.parts, spec),
                    components: [...(message.components || []), spec],
                  }
                : message,
            ),
          );
        },
        onCost: (usage) => {
          // This turn's token/cost readout (terminal cost-v1 extension metadata) — pin it to the assistant
          // message so the per-turn footer survives reload with the rest of the message.
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) =>
              message.id === assistantId ? { ...message, usage } : message,
            ),
          );
        },
        onContext: (contextWindow) => {
          // This turn's context-window fill + compaction threshold (terminal context-v1) —
          // pinned to the message so the footer meter persists with history.
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) =>
              message.id === assistantId ? { ...message, contextWindow } : message,
            ),
          );
        },
        onDone: () => {
          clearWatchdog();
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
      }, {
        images: opts.images,
        model: session.model,
        reasoningEffort: effectiveReasoningEffort(session),
        // Read live (not the render-closure session) so an "Approve & don't ask again" that
        // flips bypass on right before this resume turn is carried by it.
        bypassPermissions: chatStore.getSnapshot().sessions.find((s) => s.id === session.id)?.bypassPermissions,
        // Incognito (ADR 0069 D3b) — per-MESSAGE server-side, so read live and stamp
        // EVERY send while the tab's toggle is on (a mixed thread would leak earlier
        // incognito content into a later non-incognito turn's summary).
        incognito: chatStore.getSnapshot().sessions.find((s) => s.id === session.id)?.incognito,
        // Marks this message as the answer to the pending HITL interrupt (#1560).
        hitlResume: opts.hitlResume,
      });
      // The stream closed without the terminal canonical text (#1938): the settled
      // bubble is only this client's delta accumulation, so reconcile it against
      // the durable task — the server's artifact is the source of truth and a
      // straight REPLACE collapses any doubled/lost-chunk divergence. Skipped on
      // every healthy turn (the terminal frame sets sawAuthoritativeText).
      if (!sawAuthoritativeText && turnTaskId) {
        try {
          const res = await api.getTask(turnTaskId);
          if (/completed/i.test(res.state) && res.text) {
            const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
            if (latest) {
              chatStore.updateMessages(
                session.id,
                latest.messages.map((m) =>
                  m.id === assistantId
                    ? { ...m, content: res.text, parts: replaceText(m.parts, res.text, m.content) }
                    : m,
                ),
              );
            }
          }
        } catch {
          // Best-effort — the settled accumulation stands if the task read fails.
        }
      }
      chatStore.setSessionStatus(session.id, "idle");
      setStatusMessage("idle");
      void reconcileSteer();
    } catch (exc) {
      if (controller.signal.aborted) {
        // A user Stop OR a watchdog self-heal (which aborts to free a stalled
        // socket AFTER finalizing the turn from the durable task). In the watchdog
        // case the bubble + session state are already settled — don't clobber them
        // with "stopped"/idle.
        if (!settledByWatchdog) {
          setStatusMessage("stopped");
          chatStore.setSessionStatus(session.id, "idle");
        }
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
    } finally {
      clearWatchdog();
      abortRef.current = null;
      setTaskId("");
    }
  }

  async function stop() {
    // Release any locally-owned stream first — the server cancel is an RPC and
    // must never gate the UI stopping (#1617: Stop appeared dead while a long
    // reasoning chain streamed).
    abortRef.current?.abort();
    // The task to cancel: this slot's live turn, or — when the slot re-attached
    // to a turn it didn't start (reload / remount / the desktop relay, all of
    // which leave taskId state empty and abortRef null) — the streaming
    // message's durable taskId. Resolve BEFORE settling bubbles below, which
    // erases the `streaming` marker the fallback keys off. On desktop the relay
    // ignores the abort signal entirely, so this server-side cancel is the only
    // thing that actually halts the turn there.
    const before = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
    const cancelId = resolveStopTarget(before?.messages || [], taskId);
    // Settle the thread immediately: no bubble may stay `streaming` after Stop.
    // The send-loop only finalizes turns it owns; a re-attached turn has none.
    if (before) chatStore.updateMessages(sessionId, finalizeStoppedMessages(before.messages));
    chatStore.setSessionStatus(sessionId, "idle");
    setStatusMessage("stopped");
    // Drop any optimistic queued-steer bubbles; the user chose to stop.
    setSteerQueue([]);
    if (cancelId) {
      try {
        await api.cancelTask(cancelId);
      } catch {
        // Best-effort — the UI is already released; the task may have finished.
      }
    }
  }

  if (!session) return null;

  return (
    <div className="chat-session-slot" hidden={!visible}>
      <Conversation>
        {messages.length === 0 ? (
          <Empty icon={<TerminalSquare />} description="No messages in this session." />
        ) : (
          messages.map((message) => (
            <ChatMessageView
              key={message.id || `${message.role}-${message.createdAt}`}
              message={message}
              onCancelDelegation={cancelDelegation}
              actions={{
                copiedId,
                onCopy: copyMessage,
                onFork: forkAtMessage,
                onRewind: rewindAtMessage,
                onRegenerate: regenerate,
                onDismiss: dismissErroredMessage,
                lastAssistantId,
                regenDisabled: status === "streaming",
              }}
            />
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
        {serverTurnLabel && status !== "streaming" ? (
          // Server-initiated turn in flight (#1767): the same spinner a streaming
          // assistant shows, plus a label naming the trigger, so a background/scheduled/
          // watch turn no longer looks like a hung app. Display-only — the real answer
          // arrives via chat.resumed (ChatResumeWatch).
          <Message role="assistant">
            <span className="chat-server-turn">
              <Spinner size={15} /> {serverTurnLabel}
            </span>
          </Message>
        ) : null}
      </Conversation>

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
        {/* HITL panel (#1973): floats ABOVE the composer (absolute, anchored to
            .composer-wrap like the slash menu) so it never reflows the conversation,
            moves the composer, or jumps the scroll when it appears/resolves. No
            backdrop by design — answering usually means re-reading (and scrolling)
            the chat behind it. Other hosts (GoalsPanel) render HitlForm in-flow. */}
        {(hitl || composerForm) && (
          <div className="hitl-float">
            {hitl && (
              <HitlForm
                payload={hitl}
                busy={status === "streaming"}
                onSubmit={resumeHitl}
                onCancel={dismissHitl}
                onApproveAlways={
                  hitl.kind === "approval" && session
                    ? () => {
                        chatStore.setSessionBypassPermissions(session.id, true); // turn bypass on for this tab
                        void resumeHitl("approved"); // …and approve the pending command
                      }
                    : undefined
                }
              />
            )}
            {/* Client composer-form (#1701) — a locally-resolved form (e.g. /effort's picker),
                the same HitlForm but with a LOCAL onSubmit. Only when the agent isn't already
                holding the panel for its own HITL interrupt, so the two never collide. */}
            {!hitl && composerForm && (
              <HitlForm
                payload={composerForm.payload}
                onSubmit={(answers) => {
                  composerForm.onSubmit(answers);
                  setComposerForm(null);
                }}
                onCancel={() => {
                  composerForm.onCancel?.();
                  setComposerForm(null);
                }}
              />
            )}
          </div>
        )}
        <PromptInput
          value={draft}
          onChange={(v) => {
            setDraft(v);
            setSlashDismissed(false); // re-open the menu when the input changes
            histIndexRef.current = null; // typing detaches from history nav (readline)
            refreshSlash(); // re-parse the "/name" token at the (post-input) caret (#1530)
          }}
          // Idle → send. While a turn streams (`busy`), the field stays live: Enter
          // queues a steer into the running turn (onQueue) without stopping it, and
          // the kit renders a dedicated Stop (onStop) beside Send.
          onSubmit={() => void send()}
          busy={status === "streaming"}
          onQueue={() => void queueSteer()}
          onStop={() => void stop()}
          // Short hints only (#1699) — key/command discoverability lives in /help now, not
          // in a placeholder wall of text competing with the message being written. ("Steer
          // the agent" is also an e2e anchor — chat-steer-cancel.spec.ts.)
          placeholder={status === "streaming" ? "Steer the agent…" : "Message protoAgent…"}
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
          // Fork-registered composer actions (ADR 0061) render alongside it.
          actions={
            <>
              {registeredComposerActions().map((a) => (
                <Button
                  key={a.id}
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={a.label}
                  title={a.label}
                  onClick={() =>
                    a.run({
                      sessionId: session?.id ?? null,
                      setDraft,
                      focusComposer: () => textareaRef.current?.focus(),
                      noteToThread,
                    })
                  }
                >
                  {a.icon}
                </Button>
              ))}
              <ComposerModelSelect />
              {session?.incognito ? (
                <button
                  type="button"
                  className="composer-incognito-toggle"
                  title="Incognito is ON for this tab — turns leave no memory (no session summary, no harvest) and inject none. Click to turn it off."
                  onClick={() => chatStore.setSessionIncognito(session.id, false)}
                >
                  <Badge status="neutral">
                    <EyeOff size={12} /> incognito
                  </Badge>
                </button>
              ) : null}
              {session?.bypassPermissions ? (
                <button
                  type="button"
                  className="composer-bypass-toggle"
                  title="Bypass permissions is ON for this tab — run_command runs WITHOUT approval. Click to turn it off."
                  onClick={() => chatStore.setSessionBypassPermissions(session.id, false)}
                >
                  <Badge status="warning">bypass on</Badge>
                </button>
              ) : null}
            </>
          }
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
                  ref={index === slashSel ? activeSlashRef : undefined}
                  role="option"
                  aria-selected={index === slashSel}
                  className={`slash-item${index === slashSel ? " active" : ""}`}
                  onMouseEnter={() => setSlashIndex(index)}
                  onClick={() => completeCommand(cmd)}
                >
                  <span className="slash-title">
                    <span className="slash-name">/{cmd.name}</span>
                    {cmd.kind ? (
                      <span className="slash-kind">{cmd.kind === "plugin_command" ? "plugin" : cmd.kind}</span>
                    ) : null}
                  </span>
                  <span className="slash-desc">{cmd.description || cmd.usage}</span>
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

      <ConfirmDialog
        open={pendingRewind !== null}
        title="Rewind to here?"
        confirmLabel="Rewind"
        destructive
        onConfirm={() => {
          if (pendingRewind) void confirmRewind(pendingRewind);
          setPendingRewind(null);
        }}
        onClose={() => setPendingRewind(null)}
      >
        <p style={{ margin: 0 }}>
          This will discard everything below this message — cannot be undone.
        </p>
      </ConfirmDialog>
    </div>
  );
}

