import "./chat.css";
import { Switch } from "@protolabsai/ui/forms";
import { TabBar } from "@protolabsai/ui/navigation";
import { EyeOff } from "lucide-react";
import { useEffect, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from "react";
import { useQuery } from "@tanstack/react-query";

import { openContextMenu } from "../contextMenu";
import { useIsMobile } from "../lib/useIsMobile";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { goalsQuery, runtimeStatusQuery } from "../lib/queries";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { chatStore, useChatState } from "./chat-store";
import "./coreSlashCommands"; // registers /new, /clear, /effort via the slash-command seam (ADR 0061)
import { ChatMemoryChoices, ClearConversationDialog } from "./ClearConversationDialog";
import { exportChatToFile } from "./exportChat";
import { continueInZed } from "./continueInZed";
import { FS_ROOTS_QUERY_KEY } from "./useEditorLinker";
import { useEditorPref } from "../lib/editorPref";
import { brandName } from "../lib/brand";
import { queryClient } from "../lib/queryClient";
import { isCodePaneEnabled } from "../codeviewer/enabled";
import { useCodeViewer } from "../codeviewer/store";
import { openPublishDialog } from "./publishDialogStore";
import { useFlag } from "../flags/flags";
import { useServerTurnSessions } from "./server-turn-store";
import { useSessionsWithBackgroundWork } from "./backgroundJobStore";
import { ADD_SELECTOR, isIncognitoAddClick, trackShiftHeld } from "./shiftCue";
import { resolveGoalCloseDisposition, sessionsToClose } from "./bulkClose";
import { NO_MEMORY_CHANGE, canClearSession, defaultMemoryChoice, retireChatSession, type ChatMemoryChoice } from "./sessionRetirement";
import { ChatSessionSlot } from "./ChatSessionSlot";

// The composer predicates moved with ChatSessionSlot (#3841); re-exported so existing
// importers (ChatSurface.test.tsx) keep importing them from here.
export {
  canSubmitChatDraft,
  chatComposerBusy,
  resolveComposerStopTarget,
  serverTurnRestoreAfterFailedCancel,
} from "./ChatSessionSlot";

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
  // …and sessions whose background jobs (a delegation, a spawned subagent) are still running:
  // detached work the chat is waiting on, which the tab must not read as idle either.
  const backgroundSessions = useSessionsWithBackgroundWork();
  const currentSession = chat.sessions.find((session) => session.id === chat.currentSessionId) || null;
  const [pendingClose, setPendingClose] = useState<string | null>(null);
  // Bulk close (others/left/right): GOAL tabs still waiting for their Stop/Detach confirm AFTER
  // the one in `pendingClose`. Only goal tabs are queued — plain tabs close inline in
  // startBulkClose — so we never parade a "Delete this chat?" dialog past each tab (the
  // dialog-storm the spec warns against), and exactly one dialog is ever open.
  const [closeQueue, setCloseQueue] = useState<string[]>([]);
  // #4053: harvest an ordinary chat into the knowledge base on delete by default; the
  // pendingClose effect (below) re-initialises this from the target tab's incognito flag each
  // time the dialog opens, so an incognito chat starts OFF (and renders no harvest switch).
  const [harvestOnDelete, setHarvestOnDelete] = useState(false);
  // #3493: forget what the chat already wrote to memory (archives, harvested summaries/facts).
  const [forgetOnDelete, setForgetOnDelete] = useState(false);
  const [retiringSessionId, setRetiringSessionId] = useState<string | null>(null);
  // Goal tab close: default keeps the goal running (detach); toggle on to STOP it (clear the
  // goal + close its task backlog) instead.
  const [stopGoalOnClose, setStopGoalOnClose] = useState(false);
  const pendingCloseSession = chat.sessions.find((s) => s.id === pendingClose) || null;
  // Clear conversation (⌘K / /clear, #2996): wipe THIS tab's history but keep the tab open —
  // gated behind the same confirm+harvest dialog as delete, since it's just as destructive.
  const [pendingClear, setPendingClear] = useState<string | null>(null);
  const [clearingSessionId, setClearingSessionId] = useState<string | null>(null);
  // Active goals keyed by session — a tab whose session is driving a goal gets a different
  // close flow (detach + keep running) instead of the plain delete. Cached; refetches on
  // focus. `status: "active"` is the only in-flight state.
  const goalsState = useQuery(goalsQuery());
  const goalSessions = goalsState.data?.goals ?? [];
  const closingGoal = pendingClose
    ? goalSessions.find((g) => g.session_id === pendingClose && g.status === "active")
    : undefined;
  // Pre-release (chat.publish, ADR 0068) — gates the tab context-menu item; the /publish
  // slash command gates itself via its own `flag:` tag through the registry.
  const publishEnabled = useFlag("chat.publish");
  // "Continue in Zed" (tab menu) — offered only while the operator's external editor is Zed:
  // the hand-off is claimed by the Zed ACP shim, nothing else reads it.
  const editorPref = useEditorPref();
  const toast = useToast();
  const { data: runtimeInfo } = useQuery(runtimeStatusQuery());
  const agentDisplayName = brandName(runtimeInfo?.identity?.name);

  function handOffToZed(id: string) {
    const session = chat.sessions.find((s) => s.id === id);
    void continueInZed(
      {
        sessionId: id,
        title: session?.title,
        // Read at click time — the pane may have moved since the menu opened. Only while the
        // code pane toolset is on (ADR 0112 amendment): off, the persisted `current` is a stale
        // leftover, so the hand-off degrades to the chat alone (no project/file).
        current: isCodePaneEnabled() ? useCodeViewer.getState().current : null,
        agentName: agentDisplayName,
      },
      {
        post: (body) => api.editorHandoff(body),
        roots: async () =>
          (await queryClient.fetchQuery({ queryKey: FS_ROOTS_QUERY_KEY, queryFn: () => api.fsRoots(), staleTime: 60_000 }))
            .roots,
        navigate: (href) => {
          // Same-window hand-off like the tool-card links: a custom scheme goes to the OS
          // (the desktop shell intercepts the navigation) without unloading the page.
          window.location.href = href;
        },
        toast,
      },
    );
  }

  useEffect(() => {
    if (!chat.currentSessionId && chat.sessions.length === 0) {
      chatStore.createSession();
    }
  }, [chat.currentSessionId, chat.sessions.length]);

  // Deletes asked for outside the tab strip (the mobile SessionSheet, #2512) arrive as a
  // store-level request and fold into the SAME pendingClose dialog — harvest opt-in, server
  // purge, goal Stop-vs-Detach and all. Consumed only while no dialog is open, so a request
  // landing mid-bulk-close waits its turn instead of clobbering the queue. A stale id (the
  // session vanished, e.g. deleted from another tab) is dropped without a dialog.
  useEffect(() => {
    const requested = chat.pendingDeleteRequest;
    if (!requested || pendingClose !== null) return;
    chatStore.clearDeleteRequest();
    if (chat.sessions.some((s) => s.id === requested)) setPendingClose(requested);
  }, [chat.pendingDeleteRequest, pendingClose, chat.sessions]);

  // Re-initialise the delete dialog's memory switches from the TARGET tab's incognito flag
  // whenever the dialog opens or promotes the next queued tab (#4053) — harvest ON for an
  // ordinary chat, OFF for incognito; forget always OFF. Keyed on `pendingClose` alone, reading
  // the incognito flag from the store SNAPSHOT (not reactive `chat.sessions`) so a mid-dialog
  // sessions update can't re-fire and clobber a tick the operator just made. This is the single
  // reset point for the switches; the close helpers no longer hard-false them.
  useEffect(() => {
    if (pendingClose === null) return;
    const incognito = chatStore.getSnapshot().sessions.find((s) => s.id === pendingClose)?.incognito;
    setHarvestOnDelete(!incognito);
    setForgetOnDelete(false);
  }, [pendingClose]);

  async function closeSession(id: string, memory: ChatMemoryChoice): Promise<boolean> {
    try {
      await retireChatSession(id, memory);
      return true;
    } catch (error) {
      onError(`Couldn't delete chat: ${errMsg(error)}. The tab was kept so you can retry.`);
      return false;
    }
  }

  // Clear requests from ⌘K / /clear (both run outside React, so they park the id in the store
  // and we fold it into the confirm dialog here, #2996). Consumed only while no clear dialog is
  // open; a stale id (tab vanished) is dropped without a dialog.
  useEffect(() => {
    const requested = chat.pendingClearRequest;
    if (!requested || pendingClear !== null) return;
    chatStore.clearClearRequest();
    if (chat.sessions.some((s) => s.id === requested)) setPendingClear(requested);
  }, [chat.pendingClearRequest, pendingClear, chat.sessions]);

  async function clearSession(id: string, memory: ChatMemoryChoice): Promise<boolean> {
    if (!canClearSession(chatStore.getSnapshot().sessionStatusMap[id], serverTurnSessions.has(id))) {
      onError("Stop the active response before clearing this conversation.");
      return false;
    }
    try {
      await api.clearChatSession(id, memory.harvest, memory.forget);
      chatStore.updateMessages(id, []);
      return true;
    } catch (error) {
      onError(`Couldn't clear chat: ${errMsg(error)}. Its history was kept so you can retry.`);
      return false;
    }
  }

  // Kick off a bulk close (Close others/left/right). `ids` is the already-resolved target list
  // (sessionsToClose, anchor excluded). Split it: plain tabs close immediately, auto-harvesting
  // each unless it's incognito (#4053, matching the delete dialog's default); goal-driving tabs,
  // whose Stop-vs-Detach choice can't be defaulted safely, are queued through the SAME single-tab
  // confirm one at a time. The pendingClose effect initialises the promoted tab's switches.
  function startBulkClose(ids: string[]) {
    if (ids.length === 0) return;
    const activeGoalIds = new Set(
      goalSessions.filter((g) => g.status === "active").map((g) => g.session_id),
    );
    const goals = ids.filter((id) => activeGoalIds.has(id));
    for (const id of ids) {
      if (activeGoalIds.has(id)) continue;
      const session = chat.sessions.find((s) => s.id === id);
      void closeSession(id, defaultMemoryChoice(session?.incognito));
    }
    setStopGoalOnClose(false);
    setPendingClose(goals[0] ?? null);
    setCloseQueue(goals.slice(1));
  }

  // A close dialog resolved (confirmed): promote the next queued goal tab into the dialog, or
  // close it when the queue is drained. The pendingClose effect re-initialises the memory
  // switches from the promoted tab's incognito flag; only the goal detach toggle resets here.
  // For a single (non-bulk) close the queue is empty, so this just clears the dialog.
  function advanceClose() {
    setStopGoalOnClose(false);
    setPendingClose(closeQueue[0] ?? null);
    setCloseQueue((queue) => queue.slice(1));
  }

  // Cancel: abort the WHOLE bulk operation, not just the current tab — hitting cancel means
  // "stop closing", so the remaining queued tabs are spared. The dialog closes (pendingClose →
  // null); the next open re-initialises the switches via the pendingClose effect.
  function cancelClose() {
    if (retiringSessionId) return;
    setPendingClose(null);
    setCloseQueue([]);
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
    // Cached absence is not authoritative: the query may still be mounting or
    // refetching after a goal changed. Always refresh before the no-confirm
    // shortcut, and fail closed if ownership cannot be verified.
    void (async () => {
      const disposition = await resolveGoalCloseDisposition(session.id, async () => {
        const result = await goalsState.refetch();
        // A failed background refetch may retain previously successful data;
        // that cache is still stale and must not authorize deletion.
        return result.error === null && result.data ? result.data.goals : undefined;
      });
      // The tab may have disappeared while the authoritative read was in flight.
      if (!chatStore.getSnapshot().sessions.some((candidate) => candidate.id === session.id)) return;
      if (disposition === "confirm-goal") {
        setPendingClose(session.id);
      } else if (disposition === "direct") {
        // #1373 quick-delete: no confirm, but still auto-harvest a regular chat (#4053) —
        // incognito chats are never harvested. Never forget without the dialog's opt-in.
        await closeSession(session.id, defaultMemoryChoice(session.incognito));
      } else {
        onError("Couldn't verify whether this chat owns an active goal. The tab was kept; try again.");
      }
    })();
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
      onExport: () => void exportChatToFile(id),
      // Pre-release (chat.publish, ADR 0068): the menu item is simply absent while the
      // flag is off, same pattern as the flag-tagged slash commands below.
      onPublish: publishEnabled ? () => openPublishDialog(id) : undefined,
      // Hidden for incognito chats: a Zed thread would continue them WITHOUT the incognito
      // flag (the shim sends ordinary turns), silently writing the chat to memory.
      onContinueInZed: editorPref === "zed" && !target?.incognito ? () => handOffToZed(id) : undefined,
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
                  : serverTurnSessions.has(session.id) || backgroundSessions.has(session.id)
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
        confirmLabel={retiringSessionId
          ? "Deleting…"
          : closingGoal
            ? (stopGoalOnClose ? "Stop goal & close" : "Keep running, close tab")
            : "Delete chat"}
        destructive={!closingGoal || stopGoalOnClose}
        onConfirm={() => {
          if (!pendingClose || retiringSessionId) return;
          const id = pendingClose;
          void (async () => {
            if (closingGoal && !stopGoalOnClose) {
              // DETACH deliberately keeps the server session/goal. This is a local
              // tab dismissal, not durable retirement.
              void api.resumeGoal(id).catch(() => {});
              chatStore.dismissSession(id);
              advanceClose();
              return;
            }
            setRetiringSessionId(id);
            try {
              if (closingGoal && stopGoalOnClose) await api.clearGoal(id, true);
              // Goal closes never touch memory. Otherwise send the switch choices — forced to
              // harvest=false for an incognito chat, which renders no harvest switch (#4053).
              const memory = closingGoal
                ? NO_MEMORY_CHANGE
                : { harvest: pendingCloseSession?.incognito ? false : harvestOnDelete, forget: forgetOnDelete };
              if (await closeSession(id, memory)) advanceClose();
            } catch (error) {
              onError(`Couldn't stop and delete this goal chat: ${errMsg(error)}. The tab was kept so you can retry.`);
            } finally {
              setRetiringSessionId(null);
            }
          })();
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
              {/* Harvest defaults ON for an ordinary chat (#4053); incognito chats render no
                  harvest switch, just the "never harvested" note. The note says what compaction
                  may already have archived (#3493). Shared with the clear dialog. */}
              <ChatMemoryChoices
                action="Deleting"
                incognito={pendingCloseSession.incognito}
                harvest={harvestOnDelete}
                forget={forgetOnDelete}
                onHarvestChange={setHarvestOnDelete}
                onForgetChange={setForgetOnDelete}
              />
            </>
          )
        ) : undefined}
      </ConfirmDialog>

      {/* Clear conversation (⌘K / /clear, #2996): destructive, so it's gated behind the
          same confirm+harvest dialog as delete — but on confirm it wipes history and keeps
          the tab, rather than closing it. */}
      <ClearConversationDialog
        open={pendingClear !== null}
        incognito={chat.sessions.find((s) => s.id === pendingClear)?.incognito}
        onConfirm={(memory) => {
          if (!pendingClear || clearingSessionId) return;
          const id = pendingClear;
          setClearingSessionId(id);
          void clearSession(id, memory)
            .then((cleared) => {
              if (cleared) setPendingClear(null);
            })
            .finally(() => setClearingSessionId(null));
        }}
        onCancel={() => {
          if (!clearingSessionId) setPendingClear(null);
        }}
      />
    </section>
  );
}
