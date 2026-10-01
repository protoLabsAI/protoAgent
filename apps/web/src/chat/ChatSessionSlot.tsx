import { Badge, Button } from "@protolabsai/ui/primitives";
import { PromptInput } from "@protolabsai/ui/ai";
import { EyeOff, Users } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { useKbIntents } from "../keybindings/intents";
import { api } from "../lib/api";
import { CHAT_ATTACH_ACCEPT } from "../lib/attachTypes";
import { errMsg } from "../lib/format";
import { runtimeStatusQuery } from "../lib/queries";
import { useUI } from "../state/uiStore";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import type { ChatMessage, SystemNoteTone } from "../lib/types";
import { HitlForm } from "./HitlForm";
import { notifyIfHidden } from "../lib/notify";
import {
  chatStore,
  useChatState,
  effectiveReasoningEffort,
  sessionCast,
  subscribeGoalKickoff,
  takeGoalKickoff,
  type SessionStatus,
} from "./chat-store";
import { PublishDialog } from "./PublishDialog";
import { findSlashCommand } from "../ext/slashRegistry";
import { continueRun, leadingRun, runHas, toggleMention } from "./mentionRun";
import { insertRoomBubble } from "./roomBubble";
import type { ComposerFormSpec } from "../ext/slashRegistry";
import { registeredComposerActions } from "../ext/composerRegistry";
import { ChatTranscript } from "./ChatTranscript";
import { ComposerModelSelect } from "./ComposerModelSelect";
import {
  noteTurnFinished,
  noteTurnStarted,
  useServerTurn,
} from "./server-turn-store";
import { BackgroundWorkStrip } from "./BackgroundWorkStrip";
import { messageId } from "./messageId";
import { useAttachments } from "./useAttachments";
import { useSlashAutocomplete } from "./useSlashAutocomplete";
import { useHitl } from "./useHitl";
import { useSteerQueue } from "./useSteerQueue";
import { inputHistory, pushInputHistory } from "./inputHistory";
import { dismissedToolCallSet, rememberDismissedToolCall } from "./dismissedToolCalls";
import { registerChatEscapeHandler, resolveEscapeAction } from "./escapeStop";
import { registerSlashDispatcher } from "./slashDispatch";
import { isTaskFailed } from "./taskState";
import { resolveComposerUp } from "./queuedRecall";
import { isDuplicateRiskActive, nextDuplicateRisk, type DuplicateRisk } from "./duplicateRisk";
import { finalizeStoppedMessages, resolveStopTarget } from "./stopTurn";
import { lastOperatorAssistantId, rewindableTailId } from "./parts";
import { createRevealQueue } from "./revealQueue";
import {
  applyComponent,
  applyReasoning,
  applyText,
  applyToolEvent,
  createParkTracker,
  settleStreamEnd,
} from "./turnReducers";
import { applyDelegateProgress, settleDelegateProgress } from "./delegateProgress";
import { onLiveComponent, onLiveToolEvent } from "../codeviewer/live";
import { dispatchLiveComponent } from "../ext/componentRegistry";
import { applyCanonicalTurnText, markTurnAnsweredByParticipants, settleTurnBubbles } from "./turnText";
import {
  leadAssistantMessage,
  reattachKeyForMessages,
  reattachOrReconcile,
  settleAnsweredPause,
  unpauseBubble,
} from "./reattach";
import { beginLocalTurn, reconcileSessionStatus } from "./sessionLiveness";
import { loadDraft, loadScroll, saveDraft, saveScroll } from "./scratchState";
import { createStreamWatchdog } from "./streamWatchdog";
import { composerPlaceholder } from "./composerPlaceholder";

// A stable event function whose body always sees the latest render. Transcript props need
// stable identities so composer-only state changes can stop at the memo boundary, while
// message actions must still observe current session/status state when they are clicked.
function useLatestCallback<Args extends unknown[], Result>(callback: (...args: Args) => Result) {
  const callbackRef = useRef(callback);
  callbackRef.current = callback;
  return useCallback((...args: Args) => callbackRef.current(...args), []);
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

// The composer takes its queue-while-working `busy` shape from a turn this browser can act
// on: its OWN stream (steer) or an ATTENDED server turn (interject). An unattended server
// turn (serverTurnLabel only, no control frame) keeps the composer fully normal — the base
// behavior — so a normal send still starts a browser-owned turn. `serverTurnLabel` is kept in
// the signature for callers/tests; it deliberately does NOT gate busy on its own.
/** After a Stop whose cancel RPC REJECTED: the turn the operator asked to stop is still
 *  running server-side, but the optimistic release already dropped its Stop/interjection
 *  control — so without putting it back the operator can neither retry the cancel nor
 *  interject on their own live turn (#3092 review finding).
 *
 *  Returns what to restore, or `null` when there is nothing to put back: a locally-owned
 *  stream carries no server control and was already aborted client-side, so a failed
 *  server cancel leaves it correctly settled.
 */
export function serverTurnRestoreAfterFailedCancel<T extends { taskId?: string }>(
  control: T | null | undefined,
  label: string | null,
): { control: T; label: string } | null {
  if (!control) return null;
  return { control, label: label || "" };
}

export function chatComposerBusy(
  status: SessionStatus,
  _serverTurnLabel: string | null,
  serverTurnControl: unknown,
): boolean {
  return status === "streaming" || Boolean(serverTurnControl);
}

export function canSubmitChatDraft({
  draft,
  hasReadyAttachment,
  status,
  signedOut,
  serverTurnControl,
}: {
  draft: string;
  hasReadyAttachment: boolean;
  status: SessionStatus;
  signedOut: boolean;
  serverTurnLabel: string | null;
  serverTurnControl: unknown;
}): boolean {
  if (signedOut || status === "streaming") return false;
  // Attended server turn: Enter submits a text interjection (attachments can't ride the
  // control path), so require non-empty text.
  if (serverTurnControl) return Boolean(draft.trim());
  // Idle OR an unattended server turn: ordinary send gating (text or a ready attachment).
  return Boolean(draft.trim()) || hasReadyAttachment;
}

export function resolveComposerStopTarget(
  messages: ChatMessage[],
  liveTaskId: string,
  serverTurnControl: { taskId: string } | null | undefined,
): string {
  return serverTurnControl?.taskId || resolveStopTarget(messages, liveTaskId);
}

export function ChatSessionSlot({
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
  const [draft, setDraft] = useState(() => loadDraft(sessionId));
  // Persist the draft per session (Swap & Resume S3) — an agent switch is a full
  // navigation, and a half-written message used to vanish silently. Debounced a
  // touch so streaming keystrokes don't hammer sessionStorage.
  useEffect(() => {
    const t = setTimeout(() => saveDraft(sessionId, draft), 150);
    return () => clearTimeout(t);
  }, [draft, sessionId]);
  // Turn status is still tracked (drives the stream lifecycle) but no longer surfaced as
  // a spinner/"working…" strip above the composer — the inline indicators cover it now.
  const [, setStatusMessage] = useState("");
  const [taskId, setTaskId] = useState("");
  // The pending HITL interrupt + its answer/dismiss handlers (useHitl.ts). `lastAssistantId`
  // is a memo further down, so it goes in as a thunk; the handlers read it only from events.
  const { hitl, hitlRef, updateHitl, resumeHitl, dismissHitl } = useHitl({
    sessionId: session?.id ?? null,
    runTurn,
    noteToThread,
    lastAssistantId: () => lastAssistantId,
  });
  const abortRef = useRef<AbortController | null>(null);
  // The live turn's reveal-queue flush (#2993): stop() settles bubbles outside
  // runTurn's closure, and it must drain any withheld answer tail first.
  const revealFlushRef = useRef<(() => void) | null>(null);
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
  // Tool-call ids of CANCELLED delegations the operator ×'d out of the transcript (#3095).
  // Seeded from localStorage so a reload keeps them hidden; see dismissedToolCalls.ts.
  const [dismissedToolCalls, setDismissedToolCalls] = useState<Set<string>>(dismissedToolCallSet);
  // The message a "Rewind to here" is pending confirmation on (null = dialog closed).
  // Rewind is destructive (discards everything below), so it goes through a confirm.
  const [pendingRewind, setPendingRewind] = useState<ChatMessage | null>(null);
  // A consumed ↑-recall's known-duplicate marker (#3413): set when editQueuedSteer's dequeue
  // resolves `consumed`, pinned to the exact recalled text + this session. Drives the inline
  // duplicate-risk warning below and its clear/send-anyway actions; all transitions run
  // through the pure reducer in duplicateRisk.ts so the race/clear rules stay testable.
  const [duplicateRisk, setDuplicateRisk] = useState<DuplicateRisk | null>(null);
  // Deterministically retire the marker the moment the draft stops being the exact recalled
  // text (edited into a follow-up, cleared) or the session no longer matches — so the warning
  // never lingers on an unrelated draft and never survives a session change (#3413). The
  // functional dispatch returns the same reference while the marker still applies, so this
  // can run on every keystroke without churning renders.
  useEffect(() => {
    setDuplicateRisk((risk) => nextDuplicateRisk(risk, { type: "draft", sessionId, draft }));
  }, [draft, sessionId]);
  // Forwarded into the DS PromptInput (inputRef) — for slash-completion focus and
  // the Ctrl/⌘+Enter caret insert. The DS component owns the auto-grow.
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // Terminal-style ↑/↓ history nav (#1496): position in the shared submitted-message ring
  // (null = not navigating), and the live draft stashed when nav began (restored on ↓ past
  // the newest). Refs, not state — they change alongside a setDraft, no separate re-render.
  const histIndexRef = useRef<number | null>(null);
  const histStashRef = useRef<string>("");
  // Every submission — a send, a queued steer/interjection — joins the recall ring and
  // detaches from history nav (useSteerQueue calls this at the point the inline code did).
  function recordSubmitted(text: string) {
    pushInputHistory(text);
    histIndexRef.current = null;
    histStashRef.current = "";
  }
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
  const serverTurnControl = chat.serverTurnControls?.[sessionId] ?? null;
  // Escape-to-stop (#2968): the `chat.stop` keybinding runs outside React, so the VISIBLE
  // slot publishes its behavior on the escapeStop seam. No dep array — re-registered each
  // render so the binding always sees the CURRENT stop() closure (it reads taskId state);
  // the seam's guarded unregister makes the per-render churn safe. Streaming with steers
  // queued → peel off the newest one (LIFO); streaming with none → stop the turn; idle →
  // strict no-op (never clear the draft or blur).
  useEffect(() => {
    if (!visible) return;
    return registerChatEscapeHandler(() => {
      const action = resolveEscapeAction(status, steerQueueRef.current);
      if (action.kind === "cancel-steer") void cancelSteer(action.steerId);
      else if (action.kind === "stop") void stop();
    });
  });
  // A server-initiated turn (background push-resume / scheduled / watch fire, #1767) is
  // running into THIS session — the browser can't stream it, so show a labelled typing
  // indicator. Suppressed while this tab is itself streaming (its own spinner covers it).
  const serverTurnLabel = useServerTurn(sessionId);
  // A turn is in flight that the composer can steer or stop — EITHER this browser
  // streaming its own turn (Enter queues a steer) OR an attended server turn
  // (Enter queues an interjection through its durable control task). Named for the
  // capability rather than the source: it is not server-specific.
  const turnInterruptible = status === "streaming" || Boolean(serverTurnControl);

  // Native vision: when the active model accepts images, attached images go
  // straight to the model as multimodal parts; otherwise they take the pipeline.
  const { data: runtime } = useQuery(runtimeStatusQuery());
  // Deliberately signed out of the native OAuth provider (#2513): the send path
  // would only fail locally, so the composer swaps for a reconnect strip instead.
  const signedOut = Boolean(runtime?.graph_auth_error) && !runtime?.graph_loaded;
  const openGlobalSettings = useUI((st) => st.openGlobalSettings);
  const visionModel = Boolean(runtime?.model?.vision);
  // A configured vision model can DESCRIBE images for a text-only chat model (#1381), so an
  // image attaches via the pipeline instead of erroring.
  const imageDescribe = Boolean(runtime?.model?.image_describe);

  const {
    attachments,
    setAttachments,
    fileInputRef,
    removeAttachment,
    onDragOver: onAttachDragOver,
    onDrop: onAttachDrop,
    onPaste: onAttachPaste,
    openFilePicker,
    onFileInputChange,
  } = useAttachments({ sessionId, onError, visionModel, imageDescribe });

  // Slash-command + @-mention autocomplete (useSlashAutocomplete.ts). `runClientSlash` is
  // this slot's hoisted dispatcher below — the hook only calls it from event handlers.
  const {
    commands,
    flagOn,
    slashMatches,
    slashActive,
    slashSel,
    slashSigil,
    activeSlashRef,
    refreshSlash,
    setSlashIndex,
    setSlashDismissed,
    setSlashCtx,
    completeCommand,
    onSlashKeyDown,
  } = useSlashAutocomplete({ textareaRef, session, draft, setDraft, runClientSlash });

  // How many room exchanges this turn has delivered (#3051). Per-turn, reset at send:
  // the first fills the in-flight bubble, the rest append.
  const roomReplies = useRef(0);
  // Continue-the-conversation prefill (#3049): the leading run the in-flight turn
  // addressed, and whether an authored reply actually came back. Set at send, read at
  // done. Routing stays PER-MESSAGE and visible — the prefill is typed text the
  // operator can delete, not hidden sticky state.
  const pendingRun = useRef<string[]>([]);
  const runAnswered = useRef(false);

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

  // Publish this slot's client-slash dispatcher so a command can be run from OUTSIDE the
  // composer (#3283) — the ⌘K palette next. Exactly the escapeStop registration above:
  // gated on `visible` so the dispatch always targets the session the operator is looking
  // at, and NO dep array, because `runClientSlash` is a fresh per-render closure over the
  // draft/form/flag/serverCommands state a command needs (the seam's guarded unregister
  // makes the per-render churn safe). Deliberately NOT gated on `surfaceActive` too: this
  // slot stays mounted for the app's lifetime (#613), so keeping it registered while the
  // operator is on another rail is what lets ⌘K reach chat from anywhere.
  //
  // Which is exactly why `surfaceActive` is REPORTED instead: while the chat surface isn't
  // the active rail surface its whole <section> is `display: none`, so a command answering
  // through `noteToThread` (or opening the /effort picker in the composer panel) draws into
  // a subtree the operator can't see — a true return that shows nothing. `sessionId` rides
  // along for the same reason on the other axis: a session-less slot can't usefully run one.
  // The caller checks both and raises the surface first — see slashDispatch.ts.
  useEffect(() => {
    if (!visible) return;
    return registerSlashDispatcher({
      run: runClientSlash,
      sessionId: session?.id ?? null,
      surfaceActive,
      // The skill path (#3292): a user-facing skill is a server-side message REWRITE, so
      // the only honest outside action is to hand the operator the draft.
      // PREFIXED, never replacing. A skill directive has to LEAD the message, so prepending
      // is both the correct placement and the non-destructive one — a half-written question
      // picked into `/triage ` becomes "/triage <that question>", which is what the operator
      // meant, instead of silently vanishing. It is also what the other two draft-writers in
      // this file already do: `completeCommand` swaps only the `/name` token under the caret
      // and keeps the surrounding text, and the participant prefill (#3049) fills only an
      // empty composer. Idempotent, so picking the same row twice doesn't stutter the token.
      prefillDraft: (text: string) => {
        setDraft((d) => (d.startsWith(text) ? d : text + d));
        textareaRef.current?.focus();
      },
    });
  });

  // Runs BEFORE the DS PromptInput's Enter-to-submit (via its onKeyDown seam):
  // preventDefault to take over the key. Slash-menu nav wins while open; ⌘/Ctrl+Enter
  // inserts a newline. Plain Enter falls through → PromptInput submits (→ send()).
  function onComposerKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (onSlashKeyDown(event)) return;
    // ↑ with a message QUEUED and nothing typed pulls that message back out of the turn to
    // edit (#2837) — decided in queuedRecall.ts. Ahead of the history ring below: the live
    // queued message beats the copy of it the ring also holds.
    if (
      event.key === "ArrowUp" &&
      !event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey
    ) {
      const up = resolveComposerUp(draft, steerQueueRef.current);
      if (up.kind === "edit-queued") {
        event.preventDefault();
        void editQueuedSteer(up.steerId);
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


  // Guard unsent composer work (S3): a draft or ready attachments prompt before
  // the page unloads (agent switch, reload, close). A merely-streaming turn does
  // NOT guard — turns are server-owned and reattach on return (S0/S1).
  useEffect(() => {
    const dirty = Boolean(draft.trim()) || attachments.length > 0;
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [draft, attachments]);


  // Scroll memory (S3): returning to a transcript you had scrolled back through
  // restores your place; near-bottom clears the memory so the default stays
  // pinned-to-latest. Uses the DS Conversation's stable .pl-convo-scroll element.
  useEffect(() => {
    const root = document.getElementById(`pl-conv-${sessionId}`);
    const el = root?.querySelector<HTMLElement>(".pl-convo-scroll");
    if (!el) return;
    const saved = loadScroll(sessionId);
    if (saved !== null && el.scrollHeight > el.clientHeight) {
      // After the DS's land-at-bottom mount effect — two frames out.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        el.scrollTop = saved;
      }));
    }
    let raf = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
        saveScroll(sessionId, atBottom ? null : el.scrollTop);
      });
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- bind once per slot
  }, [sessionId]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  // Reattach an interrupted turn (Swap & Resume S1): if the last assistant
  // message is stuck in `streaming` with no live controller, resubscribe to the
  // server-owned task — the snapshot replay fills in everything missed (tool
  // cards, reasoning, text) and the live tail streams in like a normal turn.
  // Cold agents (409/502 behind the fleet proxy) are retried with backoff; a
  // turn that already ended falls back to one snapshot replay + finalize.
  // Settled-but-stale local transcripts from an agent switch are not reattached;
  // durable boot hydration repairs their rendered answer parts in the store.
  const reattachKey = reattachKeyForMessages(session?.messages);
  useEffect(() => {
    if (abortRef.current) return; // a live turn in this slot owns the stream
    // With nothing to reattach, this reconciles instead, which is what an opened sixth
    // session needs if its turn ended while its slot was not mounted (reattachOrReconcile).
    return reattachOrReconcile(sessionId, {
      onStatus: (m) => setStatusMessage(m),
      onHitl: (payload) => {
        updateHitl(payload);
        notifyIfHidden(payload.title || "protoAgent needs your input", payload.question || payload.description);
      },
    });
    // `reattachKey` changes when boot hydration fills an ALREADY-MOUNTED empty
    // fixed-id tab; sessionId alone would strand that recovered HITL/live turn.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- callbacks intentionally bind this slot
  }, [sessionId, reattachKey]);

  const messages = session?.messages || [];
  // Regenerate is offered only on the most recent OPERATOR-initiated assistant reply — a
  // server-initiated result (scheduled fire / watch / autonomous wake, #3028) is role
  // "assistant" too but is excluded, so it can't steal the slot and strip Regenerate off
  // the operator's real last reply when it lands after it.
  const lastAssistantId = useMemo(() => lastOperatorAssistantId(messages), [messages]);
  // The conversational tail — "Rewind to here" hides on it (nothing below to discard).
  const rewindTailId = useMemo(() => rewindableTailId(messages), [messages]);
  const cast = useMemo(() => (session ? sessionCast(session) : []), [session]);

  // Mid-turn steering queue + its reconcile (useSteerQueue.ts). Called HERE, where its first
  // effect (the transcript settle) used to be, so its effects keep their order relative to
  // this slot's own. Its state is declared inside the hook; nothing above reads it during
  // render (the escape handler, the ↑-recall key handler and send() read it from events).
  const {
    steerQueue,
    steerQueueRef,
    queueSteer,
    queueServerInterjection,
    dequeueSteer,
    cancelSteer,
    settleConsumed,
    reconcileSteer,
    clearQueueOnStop,
  } = useSteerQueue({
    sessionId,
    session,
    messages,
    status,
    visible,
    serverTurnControl,
    serverTurnLabel,
    draft,
    setDraft,
    onError,
    runTurn,
    hitlRef,
    abortRef,
    recordSubmitted,
  });

  // Sendable with text OR at least one ready attachment (file-only send, e.g.
  // "describe this image" with no caption). Matches the DS PromptInput gate,
  // which also enables submit when attachments are present (@protolabsai/ui ≥ 0.34).
  const canSend = useMemo(
    () =>
      canSubmitChatDraft({
        draft,
        hasReadyAttachment: attachments.some((a) => a.status === "ready"),
        status,
        signedOut,
        serverTurnLabel,
        serverTurnControl,
      }),
    [draft, attachments, serverTurnControl, serverTurnLabel, status, signedOut],
  );

  // The consumed ↑-recall warning shows only while the marker still applies to this session's
  // untouched recalled draft — the pure guard keeps the state and the UI from disagreeing (#3413).
  const showDuplicateRisk = isDuplicateRiskActive(duplicateRisk, sessionId, draft);

  async function send() {
    if (!session || signedOut) return;
    // A HITL form/question/approval is open (#1560): a fresh send would race the
    // pending form — the server holds unmarked messages anyway — so queue it as a
    // steer instead. It folds into the agent's context right AFTER the form
    // response (submit or dismiss), with the same queued-bubble + ✕ affordances
    // as mid-turn steering. Attachments stay in the tray for the next real send.
    if (hitl) {
      void queueSteer();
      return;
    }
    if (serverTurnControl) {
      void queueServerInterjection();
      return;
    }
    if (!canSend) return;
    const text = draft.trim();
    recordSubmitted(text); // record for ↑/↓ recall, then reset nav to the newest
    setDraft("");
    // The slash popover tracks the TEXTAREA's live token via keyup/click/focus
    // refreshes — a mouse click on Send fires none of those, so the stale menu
    // stayed mounted OVER the HITL form a bare command (e.g. /effort) opens and
    // intercepted its pointer events (#2492). Clear it with the draft.
    setSlashCtx(null);
    setSlashIndex(0);
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

  // ↑ on an empty composer: pull the newest queued steer OUT of the turn and back into the
  // composer to edit (#2837 — see queuedRecall.ts for why the old history-ring recall was
  // the wrong thing). Optimistic and in that order: bubble out, text in, caret at the end,
  // so the field is editable the instant the key lands. If the agent had already read it,
  // the bubble comes back and we say so rather than leaving a silent duplicate — the text
  // stays in the composer either way, because destroying an operator's in-hand edit to undo
  // our own optimism is worse than an explained duplicate they can clear or send.
  async function editQueuedSteer(id: string) {
    const item = steerQueueRef.current.find((q) => q.id === id);
    if (!item) return;
    setDraft(item.text);
    histIndexRef.current = null; // the pulled text IS the draft now, not a position in the ring
    histStashRef.current = "";
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (!ta) return;
      ta.focus();
      ta.selectionStart = ta.selectionEnd = item.text.length; // readline: edit from the end
      refreshSlash(); // the recalled text may itself start with a "/" or "@" token
    });
    const outcome = await dequeueSteer(id);
    if (outcome === "consumed") {
      // Too late — the agent already read it (dequeueSteer restored its bubble). Pin the
      // known-duplicate marker to the exact recalled text so the inline warning below makes
      // the state + consequence plain and offers clear / send-anyway; the toast stays as an
      // immediate (screen-reader) announcement, but the operator no longer RELIES on it —
      // the persistent affordance is what carries the state until they resolve it (#3413).
      setDuplicateRisk((risk) => nextDuplicateRisk(risk, { type: "recall-consumed", sessionId, text: item.text }));
      onError("The agent already read that message, so it stays in this turn. Your copy is in the composer — edit and send it as a follow-up, or clear it.");
    } else if (outcome === "removed") {
      // A clean recall is ordinary editable recall — clear any stale marker so a fresh ↑
      // never inherits a prior consumed recall's duplicate warning (#3413).
      setDuplicateRisk((risk) => nextDuplicateRisk(risk, { type: "recall-removed" }));
    }
  }

  // "Clear" on the duplicate-risk warning: drop only the recalled draft and the marker. The
  // already-consumed steer stays in the turn (its restored bubble is untouched), so it remains
  // honestly represented — this clears the composer, it does NOT unsend anything (#3413).
  function clearDuplicateRisk() {
    setDraft("");
    setDuplicateRisk((risk) => nextDuplicateRisk(risk, { type: "clear" }));
    textareaRef.current?.focus();
  }

  // "Send anyway" on the duplicate-risk warning: deliver the recalled text ONCE via the exact
  // path the composer's Enter would take — queue a steer while a turn is interruptible, a fresh
  // send when idle — and drop the marker. It never touches the already-consumed steer, so the
  // UI can't imply it can unsend it (#3413).
  function sendDespiteDuplicate() {
    setDuplicateRisk((risk) => nextDuplicateRisk(risk, { type: "sent" }));
    if (turnInterruptible) {
      void (serverTurnControl ? queueServerInterjection() : queueSteer());
    } else {
      void send();
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

  function copyMessage(message: ChatMessage) {
    void navigator.clipboard?.writeText(message.content || "");
    setCopiedId(message.id ?? null);
    window.setTimeout(() => setCopiedId((id) => (id === message.id ? null : id)), 1500);
  }

  // Regenerate an assistant reply: drop it (and anything after) from the thread,
  // then re-run the user message that prompted it via the `hidden` path — no
  // duplicate user bubble, just a fresh streaming assistant. Only offered on the
  // last assistant message when idle.
  // Dismiss a settled CANCELLED delegation card (#3095): its × on the tool card adds the
  // id to a localStorage-backed set and the render below filters the card out of THIS
  // client's view. Local-only — no backend call, the chat history keeps the full turn
  // (contrast dismissErroredMessage below, whose bubble never WAS history). Held as state
  // so the dismissal re-renders the transcript immediately; persisted so a reload doesn't
  // resurrect the card.
  function dismissToolCall(id: string) {
    setDismissedToolCalls(rememberDismissedToolCall(id));
  }

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

  async function regenerate(assistantId?: string) {
    if (!assistantId || !session || status === "streaming") return;
    const snap = chatStore.getSnapshot().sessions.find((s) => s.id === session.id);
    if (!snap) return;
    const i = snap.messages.findIndex((m) => m.id === assistantId);
    if (i < 0) return;
    const userIndex = [...snap.messages.slice(0, i)].reverse().findIndex((m) => m.role === "user");
    if (userIndex < 0) return;
    const absUserIndex = i - 1 - userIndex;
    const user = snap.messages[absUserIndex];
    // Server-side rewind FIRST (#2491): discard the old user+assistant pair from
    // the checkpoint so the resend below REPLACES the turn. Without this the
    // server appended a second identical pair while the UI hid it — history,
    // session summary, and /export silently diverged from what the chat showed.
    // Same content-occurrence disambiguation confirmRewind uses; `before` makes
    // the cut exclusive (the user message goes too — runTurn re-sends it).
    const want = (user.content || "").trim();
    const occurrence = snap.messages
      .slice(0, absUserIndex)
      .filter((m) => (m.content || "").trim() === want).length;
    try {
      const r = await api.rewindChatSession(session.id, "", user.content, occurrence, true);
      if (!r.found) {
        onError("Couldn't regenerate — the turn is no longer in the agent's live context.");
        return;
      }
    } catch (e) {
      onError(`Couldn't regenerate: ${errMsg(e)}`);
      return;
    }
    chatStore.updateMessages(session.id, snap.messages.slice(0, i));
    void runTurn(user.content, { hidden: true });
  }

  // Fork the conversation at a message: open a NEW tab/session seeded with the
  // history up to and including that message, leaving the original untouched —
  // AND fork the server-side checkpoint onto the new session's thread (#2803),
  // so the branch's agent actually REMEMBERS the history it displays. Before
  // this the fork was display-only: the tab showed the transcript while the
  // agent's checkpoint was empty (a fork that looks like memory and is amnesia).
  function forkAtMessage(message: ChatMessage) {
    if (!session) return;
    const source = session;
    const i = source.messages.findIndex((m) => m.id === message.id);
    if (i < 0) return;
    const seed = source.messages.slice(0, i + 1).map((m) => ({
      ...m,
      // a forked-from message is settled history in the new branch
      status: m.status === "streaming" ? "done" : m.status,
    }));
    const created = chatStore.createSession(); // becomes the current + active tab
    chatStore.updateMessages(created.id, seed);
    const baseTitle = source.title && source.title !== "New chat" ? source.title : "Chat";
    chatStore.renameSession(created.id, `${baseTitle} (fork)`);
    // Same occurrence discipline as rewind: client message ids never appear in the
    // checkpoint, so the server resolves by content + WHICH duplicate was clicked.
    const want = (message.content || "").trim();
    const occurrence = source.messages.slice(0, i).filter((m) => (m.content || "").trim() === want).length;
    void api
      .forkChatSession(source.id, created.id, message.id ?? "", message.content, occurrence)
      .then((res) => {
        // A refused fork still leaves a usable display-only branch — but the
        // operator must KNOW the agent can't see it, so surface the server's
        // honest status line rather than failing silently (the old behavior).
        if (!res.found) {
          chatStore.updateMessages(created.id, [
            ...(chatStore.getSnapshot().sessions.find((s) => s.id === created.id)?.messages ?? seed),
            {
              id: `sys-fork-${Date.now()}`,
              role: "system",
              content: `⚠️ ${res.message}`,
              noteTone: "warning",
              createdAt: Date.now(),
              status: "done",
            } as ChatMessage,
          ]);
        }
      })
      .catch((e) => {
        chatStore.updateMessages(created.id, [
          ...(chatStore.getSnapshot().sessions.find((s) => s.id === created.id)?.messages ?? seed),
          {
            id: `sys-fork-${Date.now()}`,
            role: "system",
            content: `⚠️ Server-side fork failed — this branch is a display copy the agent can't see. (${errMsg(e)})`,
            noteTone: "warning",
            createdAt: Date.now(),
            status: "done",
          } as ChatMessage,
        ]);
      });
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
    // Who this message addresses (its leading run) — for the continue prefill at done.
    pendingRun.current = leadingRun(content).names;
    runAnswered.current = false;
    const assistantId = opts.resumeMessageId ?? messageId();
    roomReplies.current = 0; // per-turn (#3051) — a fresh turn starts with an empty room
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
    // A HITL answer continues the task that parked (A2A §3.4.3, #3930): the bubble it
    // resumes, or the lead turn's latest one. The server re-routes a stale id itself.
    const pausedBubble = opts.hitlResume
      ? opts.resumeMessageId
        ? base.find((m) => m.id === opts.resumeMessageId)
        : leadAssistantMessage(base)
      : undefined;
    const pausedTaskId = pausedBubble?.taskId;
    // `hidden` (an approval resume, or a regenerate) sends `content` to the server but
    // omits the user bubble — the agent still receives it, the chat just doesn't show it.
    // A resume flips the SAME assistant message back to streaming (keeping its parts/toolCalls).
    chatStore.updateMessages(
      session.id,
      resuming
        ? base.map((m) => (m.id === assistantId ? { ...unpauseBubble(m), status: "streaming" } : m))
        : opts.hidden
          ? [...base, assistant]
          : [
              // A form/question answer continues in a new bubble: the one that paused is done.
              ...(opts.hitlResume ? settleAnsweredPause(base, pausedBubble?.id) : base),
              userMessage,
              assistant,
            ],
    );
    chatStore.setSessionStatus(session.id, "streaming");
    onError("");

    const controller = new AbortController();
    abortRef.current = controller;
    // From the top of the `try` until `finally`, this session's "streaming" is this turn's,
    // even while no bubble reads streaming: onDone settles the bubble before the post-stream
    // GetTask reconcile below, and a pure fan-out folds the placeholder away. The reconciler
    // must not idle it then, or Send comes back mid-turn and starts a second stream in this
    // slot. It is claimed inside the `try` and released first in `finally`: a claim that
    // outlived a throw would block the reconciler for this session for good.
    let endLocalTurn: () => void = () => {};

    // Whether the stream delivered an AUTHORITATIVE full-turn text (a replace —
    // the terminal artifact's append:false canonical re-send, or a terminal task
    // frame). When the connection drops that frame (a proxy/tailnet blip at turn
    // end), the settled bubble is only the client's own delta accumulation — any
    // divergence (a lost or doubly-delivered chunk, #1938) would persist as
    // done-but-wrong with nothing left to correct it. Track it so the settle path
    // below can reconcile against the durable task exactly when it's needed.
    let sawAuthoritativeText = false;
    let turnTaskId = "";
    // The turn PARKED on the operator (#3956): the stream reported input-required (an
    // `ask_human` question, a form, an approval) or auth-required. The SDK closes the stream
    // there, but the turn is not over — the answer continues the same task — so the close
    // leaves the bubble streaming + paused rather than settling it done. A plugin composer
    // form rides the same frame but parks no graph (its redeem completes the task
    // server-side), so it settles as before.
    const park = createParkTracker();

    // Reveal queue (#2993): streamed answer deltas don't render the instant
    // their frame arrives — they drip out at a steady ~word cadence. Diagnosis
    // (measured; see revealQueue.ts and server/chat.py's [stream-delta] log):
    // the Claude OAuth SDK delivers multi-word chunks upstream of the server's
    // executor, so its (correct) flush logic can't smooth them — only the
    // renderer can. Everything that needs text at its true position or must
    // settle the turn flushes the queue first: tool / reasoning / component
    // frames (part ordering), the terminal REPLACE frame, the watchdog
    // finalize, Stop, and the turn's `finally` — so the final answer is never
    // delayed by the pacing.
    const reveal = createRevealQueue({
      apply: (text) => {
        const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
        if (!latest) return;
        chatStore.updateMessages(
          session.id,
          latest.messages.map((message) => {
            if (message.id !== assistantId) return message;
            const next = applyText(message, text, true);
            // A late drip must never resurrect a bubble something else already
            // settled (Stop / watchdog): keep the terminal status, land the text.
            return message.status === "streaming" ? next : { ...next, status: message.status };
          }),
        );
      },
    });
    revealFlushRef.current = reveal.flush;

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
      const failed = isTaskFailed(state);
      const latest = chatStore.getSnapshot().sessions.find((s) => s.id === session.id);
      if (latest) {
        const now = Date.now();
        // The task's text is the whole TURN's canonical answer, and a turn can span
        // several bubbles once a steer/delegation split it — so distribute it across
        // them rather than re-landing all of it on the live one (turnText.ts).
        const reconciled = text ? applyCanonicalTurnText(latest.messages, assistantId, text) : latest.messages;
        chatStore.updateMessages(
          session.id,
          settleTurnBubbles(
            reconciled.map((m) => {
              if (m.id !== assistantId) return m;
              const toolCalls = m.toolCalls?.map((c) =>
                c.status === "running"
                  ? { ...c, status: "done" as const, durationMs: c.durationMs ?? (c.startedAt !== undefined ? now - c.startedAt : undefined) }
                  : c,
              );
              return { ...m, status: failed ? "error" : "done", toolCalls };
            }),
            assistantId,
          ),
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
        // Reveal the withheld tail first so the finalize compares the task text
        // against the COMPLETE client accumulation.
        reveal.flush();
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
      endLocalTurn = beginLocalTurn(session.id);
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
          reveal.flush(); // settle what streamed before the error overwrites the bubble
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
                item.id === assistantId
                  ? // Keep what the turn said before it failed (#3940) — a failed workflow
                    // streams its whole output, then a one-line error; the error goes under it.
                    { ...item, content: item.content?.trim() ? `${item.content.trimEnd()}\n\n${friendly}` : friendly, status: "error" }
                  : item,
              ),
            );
          }
        },
        onInputRequired: (payload) => {
          park.inputRequired(payload);
          updateHitl(payload);
          // Alert natively if the window is hidden/unfocused (menu-bar-only
          // desktop, or a backgrounded tab) so the form isn't missed.
          notifyIfHidden(
            payload.title || "protoAgent needs your input",
            payload.question || payload.description,
          );
        },
        onTaskState: (state) => {
          // The latest state wins: a turn that parks is marked paused at once, so its
          // in-flight card reads "waiting for you" while the form is up (#3956).
          // A working state after a park un-parks the bubble (the turn is producing again).
          const transition = park.taskState(state);
          if (!transition) return;
          if (transition === "parked") reveal.flush();
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((m) =>
              m.id !== assistantId ? m : transition === "parked" ? settleStreamEnd(m, { parked: true }) : unpauseBubble(m),
            ),
          );
        },
        onText: (text, append) => {
          bumpWatchdog();
          if (append) {
            // Streamed delta — paced word-by-word through the reveal queue.
            reveal.push(text);
            return;
          }
          // A REPLACE (the turn's first frame, or the terminal canonical
          // re-send, #1709) is authoritative: reveal anything still queued
          // first — so the canonical compare sees the complete accumulation
          // instead of "diverging" and rebuilding — then land it instantly.
          // The final answer is never delayed by the queue.
          sawAuthoritativeText = true;
          reveal.flush();
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          // Spans the whole TURN, which may already have been split into several
          // bubbles to place a consumed steer / delegation (turnText.ts) — and lands
          // NOTHING on a turn whose answer the addressed participants already spoke
          // (#3449). That refusal lives there, not here, because four other producers of
          // this same text run after the stream is gone (watchdog, reconcile, reattach,
          // boot hydration) and all five pass through that one function.
          chatStore.updateMessages(session.id, applyCanonicalTurnText(latest.messages, assistantId, text));
        },
        onReasoning: (delta) => {
          bumpWatchdog();
          reveal.flush(); // part ordering — the reasoning run opens AFTER the text already streamed
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) => (message.id === assistantId ? applyReasoning(message, delta) : message)),
          );
        },
        onToolCall: (evt) => {
          bumpWatchdog();
          reveal.flush(); // part ordering — the tool card opens AFTER the text already streamed
          // `show_component` is a render directive, not a real action — its output IS the
          // inline component (delivered via onComponent / message.components). Suppress its
          // tool card so it doesn't add noise to the collapsed work timeline (#1323).
          if (evt.name === "show_component") return;
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          const next = latest.messages.map((message) =>
            message.id === assistantId ? applyToolEvent(message, evt) : message,
          );
          chatStore.updateMessages(session.id, next);
          // Follow mode (ADR 0112) — the LIVE path only. The args live on the card (the
          // second start frame carries them); the end frame need not repeat them.
          if (evt.phase === "end") {
            const card = next.find((m) => m.id === assistantId)?.toolCalls?.find((c) => c.id === evt.id);
            onLiveToolEvent(evt, card?.input, session.id);
          }
        },
        onComponent: (spec) => {
          reveal.flush(); // part ordering — the component lands AFTER the text already streamed
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          chatStore.updateMessages(
            session.id,
            latest.messages.map((message) => (message.id === assistantId ? applyComponent(message, spec) : message)),
          );
          // A `code-ref` (show_code) opens the code pane — here, on the live stream, and never
          // on hydration/replay, where the same component re-renders from history.
          onLiveComponent(spec, session.id);
          // …and any registered kind's live hook (#3617 — the artifact-ref chip opens the
          // Artifact panel on the version the agent just wrote). Same rule: live stream only.
          dispatchLiveComponent(spec, session.id);
        },
        onRoomReply: (reply) => {
          // A delegation rendered inline as a mini-conversation (#3042): the lead's
          // outgoing ASK (`addressedTo`, no author — the lead speaking to a participant),
          // then the participant's REPLY (`author`). Each lands at the point in the stream
          // where the delegate_to happened, via insertRoomBubble — which splits the lead's
          // single streaming message so a bubble never floats above the work that preceded
          // it. A `@x @y` fan-out is the same path with an empty placeholder (insert-before).
          //
          // A third shape arrives on an addressed turn: the ROOM's own note (#3449) — no
          // author, no `addressedTo`, just the part of the answer no participant's bubble
          // carries (a clipped catch-up, the round cap, a failed address's line). It falls
          // through this handler deliberately and lands as a plain, un-bylined bubble:
          // it is the room speaking about its own bounds, not a participant, and it must
          // never claim the answer (it is what the answer has BEYOND the bubbles).
          reveal.flush(); // part ordering (like onToolCall/onComponent): commit the lead's
          // streamed-so-far text into the placeholder BEFORE the split reads it — otherwise
          // the text is still buffered, the placeholder looks empty, every bubble takes the
          // insert-before path, and all the lead's prose flushes in at the END (the bug).
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          if (reply.author) runAnswered.current = true; // a real reply — the continue prefill may fire

          if (!reply.text && reply.author) {
            // A pre-fan-out server frame carries no text — the single-address case, where
            // the terminal `done` text IS the reply. Stamp the byline on the live bubble.
            chatStore.updateMessages(
              session.id,
              latest.messages.map((message) =>
                message.id === assistantId ? { ...message, author: reply.author } : message,
              ),
            );
            return;
          }
          if (!reply.text) return; // an ask with no query — nothing to show

          roomReplies.current += 1;
          // Does this bubble render words the turn's canonical answer text also carries
          // (#3449)? Then the turn is stamped below and no producer of that text will
          // land it. Three conditions, all load-bearing:
          //   - a REPLY, not an ask (an ask's text is the question) and not the room's
          //     own note (which is the part of the answer NO bubble carries);
          //   - the server claimed it (`inAnswer`);
          //   - the OPERATOR did the addressing. A reply the LEAD addressed
          //     (`delegate_to`, `from: "assistant"`) can never be the whole of the
          //     turn's answer — the lead's answer is its own synthesis — so honouring a
          //     claim there would delete the lead's words. The mention path is the only
          //     producer that sets both, and nothing stops a fork or plugin from
          //     emitting room frames, so this is checked rather than assumed.
          const claimsAnswer = Boolean(reply.author) && reply.inAnswer === true && reply.from === "operator";
          const authored: ChatMessage = {
            id: messageId(),
            role: "assistant",
            content: reply.text,
            createdAt: Date.now(),
            status: "done",
            ...(reply.author ? { author: reply.author } : {}),
            ...(reply.addressedTo ? { addressedTo: reply.addressedTo } : {}),
            ...(reply.delegation ? { delegation: reply.delegation } : {}),
            // Bubbles that carry the turn's own answer carry its task id too, so Copy /
            // Fork / Rewind / View prompt land on the message that actually shows the
            // answer (#3449 C). The turn's other half is a work card with no prose, and
            // the action row needs `content`. Read paths that group a turn by task id
            // all skip `author`-bearing bubbles by design, so this cannot make one an
            // anchor for canonical text — `turnBubbleIndexes` keys on `splitOf`, never
            // on the task.
            ...(claimsAnswer && turnTaskId ? { taskId: turnTaskId } : {}),
          };
          const withBubble = insertRoomBubble(latest.messages, assistantId, authored, messageId());
          chatStore.updateMessages(
            session.id,
            claimsAnswer ? markTurnAnsweredByParticipants(withBubble, assistantId) : withBubble,
          );
        },
        onDelegateProgress: (evt) => {
          // A coding delegate's live state (#3979) lands on its card — the `@` mention
          // card, or the `delegate_to` ask row — wherever the split put it. Not a
          // chronology frame: nothing is inserted, so no reveal flush.
          bumpWatchdog();
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          const next = applyDelegateProgress(latest.messages, evt);
          if (next !== latest.messages) chatStore.updateMessages(session.id, next);
        },
        onSteerConsumed: (consumed) => {
          // Like a room reply, this is a chronology frame inside one long assistant
          // message. Commit reveal-paced text first, then split at the exact boundary;
          // continued reasoning/tools/text keep streaming into the reset placeholder.
          reveal.flush();
          settleConsumed(consumed, assistantId);
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
          // Stream over — reveal everything still queued before the settle
          // below, so the done bubble never withholds tail text (#2993).
          reveal.flush();
          // Mid-conversation with a participant, re-typing the address every message is
          // friction — prefill the run that was just ANSWERED (#3049). Only when their
          // reply actually came back (a failed address prefills nothing), only into an
          // empty composer, and only while this tab is still the one in front. Deleting
          // the prefix is the visible, one-gesture way to talk to the lead again — and a
          // bare send clears pendingRun, so the lead conversation doesn't re-prefill.
          if (
            runAnswered.current &&
            pendingRun.current.length &&
            chatStore.getSnapshot().currentSessionId === session.id
          ) {
            const run = continueRun(pendingRun.current);
            setDraft((d) => (d.trim() ? d : run));
          }
          const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
          if (!latest) return;
          if (park.parked) {
            // Parked, not over (#3956): the bubble stays streaming + paused — waiting now,
            // and the bubble a reload's reattach resubscribes to its own task through.
            chatStore.updateMessages(
              session.id,
              latest.messages.map((m) => (m.id === assistantId ? settleStreamEnd(m, { parked: true }) : m)),
            );
            return;
          }
          const placeholder = latest.messages.find((m) => m.id === assistantId);
          const placeholderEmpty =
            !placeholder?.content && !placeholder?.parts?.length && !placeholder?.toolCalls?.length;
          if (roomReplies.current > 0 && placeholderEmpty) {
            // Pure fan-out (`@x @y`, #3051): the lead never ran, every word of the turn
            // was authored room messages, and the terminal `done` text is just the
            // combined fallback — so drop the empty live bubble rather than show it twice.
            // But when the lead DID run (it moderated a collaboration via delegate_to,
            // #3042/#3114), its synthesis is IN this bubble and must stay: the room
            // bubbles are the participants, this is the lead's own answer. The
            // placeholder-empty guard is exactly that distinction.
            //
            // It is only ABLE to make it because `applyCanonicalTurnText` refused to land
            // the answer on a turn the participants answered (#3449). Read against a
            // bubble that had taken that text, this test answered "not empty" for every
            // addressed turn — and since #3151 the address's own work card said so too —
            // which is how a dead guard let the doubled answer through for three weeks.
            // The stamp is the fact; this is the cleanup the fact makes correct again.
            //
            // FOLD, not delete (#3449 C): `onCost`/`onContext` pin this turn's spend and
            // context-window meter to the live bubble, and a raw filter dropped the whole
            // footer with it. `settleTurnBubbles` carries usage/context/error status onto
            // the half that survives — the same move a spent steer continuation gets.
            chatStore.updateMessages(session.id, settleTurnBubbles(latest.messages, assistantId));
            return;
          }
          chatStore.updateMessages(
            session.id,
            // A turn split to place a steer/delegation can end with NOTHING after the
            // split — the agent said everything before it consumed the interjection —
            // leaving a continuation that opened for text which never came. Settling
            // that draws a blank row under the answer, so fold it away (turnText.ts).
            // Same move as the pure-fan-out drop above, for the same reason.
            // A completed turn can't have tools still running: a tool_end frame that
            // races with the terminal `done` (e.g. a workflow card whose end arrives in
            // the same tick) would otherwise leave the card spinning forever —
            // settleStreamEnd flips any lingering `running` card to `done`.
            // …and a delegation row whose delegate never sent its final snapshot (a
            // stopped turn) stops reading as live (#3979).
            settleTurnBubbles(
              settleDelegateProgress(
                latest.messages.map((message) =>
                  message.id === assistantId ? settleStreamEnd(message, { parked: false }) : message,
                ),
              ),
              assistantId,
            ),
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
        taskId: pausedTaskId,
        // What this send looked like in the transcript, carried into the durable turn so
        // a rebuilt chat (ADR 0104) draws the same user bubble — none for a hidden send,
        // the typed text + 📎 list rather than the prepended attachment context.
        hidden: opts.hidden,
        display: !opts.hidden && content !== sent ? content : undefined,
      });
      // Stream returned: reveal any withheld tail NOW, before the reconcile
      // below — flushing after it would append the tail on top of the
      // canonical replace and double that text. (Redundant after onDone, which
      // already flushed; this covers a stream that closed without one.)
      reveal.flush();
      // The stream closed without the terminal canonical text (#1938): the settled
      // bubble is only this client's delta accumulation, so reconcile it against
      // the durable task — the server's artifact is the source of truth and a
      // straight REPLACE collapses any doubled/lost-chunk divergence. Skipped on
      // every healthy turn (the terminal frame sets sawAuthoritativeText).
      if (!sawAuthoritativeText && turnTaskId && !park.parked) {
        try {
          const res = await api.getTask(turnTaskId);
          if (/completed/i.test(res.state) && res.text) {
            const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
            if (latest) {
              chatStore.updateMessages(
                session.id,
                settleTurnBubbles(applyCanonicalTurnText(latest.messages, assistantId, res.text), assistantId),
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
      } else if (park.parked) {
        // The stream failed AFTER the turn parked (#3956 review): a dropped socket or an
        // error frame behind the input-required one. The turn is still parked server-side —
        // the form is up and its answer continues the task — so settle it as parked, not as
        // an error: streaming + paused with its task id, which a reload reattaches through.
        reveal.flush();
        const latest = chatStore.getSnapshot().sessions.find((item) => item.id === session.id);
        if (latest) {
          chatStore.updateMessages(
            session.id,
            latest.messages.map((m) => (m.id === assistantId ? settleStreamEnd(m, { parked: true }) : m)),
          );
        }
        chatStore.setSessionStatus(session.id, "idle");
        setStatusMessage("idle");
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
      // First, so nothing below can throw past it and leak the claim.
      endLocalTurn();
      // Whatever path unwound (done / error / abort / watchdog), never strand
      // withheld text in the reveal queue. Already-settled bubbles keep their
      // terminal status (the apply's status guard).
      reveal.flush();
      revealFlushRef.current = null;
      clearWatchdog();
      abortRef.current = null;
      setTaskId("");
      // The stream's end: every exit above settles the status itself, so this is a no-op
      // unless something left "streaming" behind with nothing live to settle it.
      reconcileSessionStatus(session.id);
    }
  }

  async function stop() {
    // Release any locally-owned stream first — the server cancel is an RPC and
    // must never gate the UI stopping (#1617: Stop appeared dead while a long
    // reasoning chain streamed).
    abortRef.current?.abort();
    // Drain the reveal queue BEFORE settling bubbles below: finalizeStoppedMessages
    // flips streaming→done, and text the server already delivered must not stay
    // withheld behind the word-cadence drip (#2993).
    revealFlushRef.current?.();
    // The task to cancel: this slot's live turn, or — when the slot re-attached
    // to a turn it didn't start (reload / remount / the desktop relay, all of
    // which leave taskId state empty and abortRef null) — the streaming
    // message's durable taskId. Resolve BEFORE settling bubbles below, which
    // erases the `streaming` marker the fallback keys off. On desktop the relay
    // ignores the abort signal entirely, so this server-side cancel is the only
    // thing that actually halts the turn there.
    const before = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
    const control = chatStore.getSnapshot().serverTurnControls[sessionId];
    // Captured before the release below clears it — needed to restore the turn label if
    // the cancel RPC fails and the turn turns out to still be live.
    const labelBeforeStop = serverTurnLabel;
    const cancelId = resolveComposerStopTarget(before?.messages || [], taskId, control);
    // Settle the thread immediately: no bubble may stay `streaming` after Stop.
    // The send-loop only finalizes turns it owns; a re-attached turn has none.
    if (before) chatStore.updateMessages(sessionId, finalizeStoppedMessages(before.messages));
    if (control) {
      chatStore.clearServerTurnControl(sessionId, control.taskId);
      noteTurnFinished(sessionId);
    }
    chatStore.setSessionStatus(sessionId, "idle");
    setStatusMessage("stopped");
    // Clear the optimistic queued bubbles; the user chose to stop. Their TEXT is not
    // discarded, and the server's copy is not left behind — see useSteerQueue's dropQueuedOnStop.
    clearQueueOnStop();
    if (cancelId) {
      try {
        await api.cancelTask(cancelId);
      } catch {
        // The cancel did NOT land, so an attended server turn is STILL RUNNING — but the
        // optimistic release above already dropped its Stop/interjection control, leaving
        // a live task with no affordance to reach it: the operator can neither retry the
        // cancel nor interject, on the very turn they asked to stop. Restore the control
        // (and its label) so the turn stays reachable. A locally-owned stream is already
        // aborted client-side and needs no restore — only the server-controlled turn does.
        const restore = serverTurnRestoreAfterFailedCancel(control, labelBeforeStop);
        if (restore) {
          chatStore.setServerTurnControl(restore.control);
          if (restore.label) noteTurnStarted(sessionId, restore.label);
        }
      }
    }
  }

  const transcriptCancelDelegation = useLatestCallback(cancelDelegation);
  const transcriptDismissToolCall = useLatestCallback(dismissToolCall);
  const transcriptCopyMessage = useLatestCallback(copyMessage);
  const transcriptForkAtMessage = useLatestCallback(forkAtMessage);
  const transcriptRewindAtMessage = useLatestCallback(rewindAtMessage);
  const transcriptRegenerate = useLatestCallback(regenerate);
  const transcriptDismissErroredMessage = useLatestCallback(dismissErroredMessage);
  const transcriptCancelSteer = useLatestCallback(cancelSteer);
  const transcriptActions = useMemo(
    () => ({
      copiedId,
      sessionId: session?.id,
      onCopy: transcriptCopyMessage,
      onFork: transcriptForkAtMessage,
      onRewind: transcriptRewindAtMessage,
      onRegenerate: transcriptRegenerate,
      onDismiss: transcriptDismissErroredMessage,
      lastAssistantId,
      regenDisabled: status === "streaming",
      incognito: session?.incognito,
      rewindTailId,
    }),
    [
      copiedId,
      lastAssistantId,
      rewindTailId,
      session?.id,
      session?.incognito,
      status,
      transcriptCopyMessage,
      transcriptDismissErroredMessage,
      transcriptForkAtMessage,
      transcriptRegenerate,
      transcriptRewindAtMessage,
    ],
  );

  if (!session) return null;

  return (
    <div className="chat-session-slot" hidden={!visible}>
      <ChatTranscript
        sessionId={sessionId}
        messages={messages}
        dismissedToolCalls={dismissedToolCalls}
        actions={transcriptActions}
        steerQueue={steerQueue}
        serverTurnLabel={serverTurnLabel}
        status={status}
        onCancelDelegation={transcriptCancelDelegation}
        onDismissToolCall={transcriptDismissToolCall}
        onCancelSteer={transcriptCancelSteer}
      />

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
        onDragOver={onAttachDragOver}
        onDrop={onAttachDrop}
      >
        {/* Who is in this chat (#3049) — derived from the transcript, so it can never
            drift from what happened (the tracked-list version grew chips a deleted draft
            left behind). A quiet legend, not presence: nothing here "listens" — an agent
            acts only when addressed, and clicking a name is exactly that affordance
            (inserts `@name `). No remove control: history is not removable, and an X
            that gated nothing was confusion pretending to be a control. */}
        <BackgroundWorkStrip sessionId={sessionId} />
        {cast.length ? (
          <div className="chat-roster" aria-label="In this chat">
            <Users size={13} aria-hidden />
            {cast.map((name) => {
              const active = runHas(draft, name);
              return (
                <button
                  key={name}
                  type="button"
                  className={`chat-roster-chip${active ? " chat-roster-chip--active" : ""}`}
                  aria-pressed={active}
                  title={active ? `Remove @${name} from this message` : `Address @${name}`}
                  onClick={() => {
                    // Chips COMPOSE the draft's leading run — the only position where a
                    // mention routes. Click to add, click again to remove; the message
                    // body is preserved either way. (The first cut refused a non-empty
                    // draft, which made every chip after the first a silent no-op.)
                    setDraft((d) => toggleMention(d, name));
                    textareaRef.current?.focus();
                  }}
                >
                  <span className="chat-roster-name">@{name}</span>
                </button>
              );
            })}
          </div>
        ) : null}
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
                  // Not for a gate that can't be session-approved (it moves the fence, or
                  // it is a floor bypass can't skip), nor one with its own choices.
                  hitl.kind === "approval" && session && hitl.session_allow !== false && !hitl.options?.length
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
        {/* Known-duplicate warning for a consumed ↑-recall (#3413): a persistent inline strip
            (mirrors the .composer-signed-out send-area pattern), not a transient toast. It
            names the state ("already delivered in this turn") and the consequence (an unchanged
            send would deliver a second copy), and offers deliberate actions — clear the draft,
            or send it anyway once. It deliberately offers no "unsend": the consumed steer
            already shaped the reply and stays in the turn. */}
        {showDuplicateRisk ? (
          <div className="composer-dup-risk" aria-label="Duplicate message warning">
            <div className="composer-dup-risk-copy">
              <strong>Already delivered in this turn.</strong> The agent already read this
              message, so it stays in the turn. Sending it again unchanged would deliver a
              second copy.
            </div>
            <div className="composer-dup-risk-actions">
              <Button size="sm" variant="ghost" onClick={clearDuplicateRisk}>
                Clear draft
              </Button>
              <Button size="sm" variant="primary" onClick={sendDespiteDuplicate}>
                Send anyway
              </Button>
            </div>
          </div>
        ) : null}
        {signedOut ? (
          /* Deliberate OAuth signed-out state (#2513): the composer is out of
             service — an enabled Send would only fail locally. Reconnect opens
             Settings → Model (the OAuth account section, #2460). */
          <div className="composer-signed-out">
            <span className="composer-signed-out-copy">
              {runtime?.graph_auth_error?.message ||
                `Signed out of ${runtime?.graph_auth_error?.provider ?? "the model provider"} — reconnect to chat.`}
            </span>
            <Button size="sm" variant="primary" onClick={() => openGlobalSettings("model")}>
              Reconnect
            </Button>
          </div>
        ) : (
        <PromptInput
          value={draft}
          onChange={(v) => {
            setDraft(v);
            setSlashDismissed(false); // re-open the menu when the input changes
            histIndexRef.current = null; // typing detaches from history nav (readline)
            refreshSlash(); // re-parse the "/name" token at the (post-input) caret (#1530)
          }}
          // Idle → send. While this browser streams, Enter queues a steer. While an
          // attended server turn is live, Enter queues an interjection through its
          // durable server-control task id instead of starting a competing turn.
          onSubmit={() => void send()}
          busy={chatComposerBusy(status, serverTurnLabel, serverTurnControl)}
          onQueue={
            turnInterruptible
              ? () => void (serverTurnControl ? queueServerInterjection() : queueSteer())
              : undefined
          }
          onStop={turnInterruptible ? () => void stop() : undefined}
          // Short hints only (#1699) — key/command discoverability lives in /help now, not
          // in a placeholder wall of text competing with the message being written. ("Steer
          // the agent" is also an e2e anchor — chat-steer-cancel.spec.ts.) With a steer
          // queued mid-turn, the hint flips to ↑-recall discoverability (#2837).
          placeholder={
            serverTurnControl
              ? "Interject into the running server task…"
              : composerPlaceholder(status, steerQueue.length)
          }
          inputRef={textareaRef}
          onKeyDown={onComposerKeyDown}
          onPaste={onAttachPaste}
          onAttach={openFilePicker}
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
                    <span className="slash-name">
                      {slashSigil ?? "/"}
                      {cmd.name}
                    </span>
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
        )}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          accept={CHAT_ATTACH_ACCEPT}
          onChange={onFileInputChange}
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

      <PublishDialog />
    </div>
  );
}
