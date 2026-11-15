import "./chat.css";
import { StatusDot } from "@protolabsai/ui/data";
import { Switch } from "@protolabsai/ui/forms";
import { TabBar, type TabBarItem } from "@protolabsai/ui/navigation";
import { EyeOff, MessagesSquare } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from "react";
import { useQuery } from "@tanstack/react-query";

import { openContextMenu } from "../contextMenu";
import { useIsMobile } from "../lib/useIsMobile";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { goalsQuery, runtimeStatusQuery } from "../lib/queries";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { Kbd } from "@protolabsai/ui/primitives";
import { chatStore, useChatState } from "./chat-store";
import "./coreSlashCommands"; // registers /new, /clear, /effort via the slash-command seam (ADR 0061)
import { ChatMemoryChoices, ClearConversationDialog } from "./ClearConversationDialog";
import { exportChatToFile } from "./exportChat";
import { continueInZed } from "./continueInZed";
import { FS_ROOTS_QUERY_KEY } from "./useEditorLinker";
import { useEditorPref } from "../lib/editorPref";
import { getBulkCloseHarvest } from "../lib/bulkCloseHarvestPref";
import { brandName } from "../lib/brand";
import { queryClient } from "../lib/queryClient";
import { isCodePaneEnabled } from "../codeviewer/enabled";
import { useCodeViewer } from "../codeviewer/store";
import { serverTurnLabel, useServerTurnSessions } from "./server-turn-store";
import { useSessionsWithBackgroundWork } from "./backgroundJobStore";
import { ADD_SELECTOR, isIncognitoAddClick, trackShiftHeld } from "./shiftCue";
import { isEmptyChatSession, resolveGoalCloseDisposition, sessionsToClose } from "./bulkClose";
import { NO_MEMORY_CHANGE, canClearSession, defaultMemoryChoice, retireChatSession, type ChatMemoryChoice } from "./sessionRetirement";
import { pushClose, pushRename } from "./sessionListSync";
import { ChatSessionSlot } from "./ChatSessionSlot";
import { ConversationsPane } from "./ConversationsPane";
import {
  CONVERSATIONS_STAGE_ID,
  isConversationsStage,
  useConversations,
  hide as hideConversationsTab,
  show as showConversationsTab,
  setStage as setConversationsStage,
  openChat as leaveConversationsForChat,
} from "./conversations-store";

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
  // "Other conversations" (docs/design/other-conversations.md, slice C2): the agent's outside
  // A2A threads, fed by C1's client-only store. Drives the pinned Conversations tab (shown only
  // once the list has loaded rows) and, while that tab is selected, the read-only list pane that
  // replaces the chat in the main pane — `currentSessionId` is never touched (§4.7).
  const conv = useConversations();
  const currentSession = chat.sessions.find((session) => session.id === chat.currentSessionId) || null;
  const [pendingClose, setPendingClose] = useState<string | null>(null);
  // Bulk close (others/left/right): GOAL tabs still waiting for their Stop/Detach confirm AFTER
  // the one in `pendingClose`. Only goal tabs are queued — plain tabs close inline in
  // startBulkClose — so we never parade a "Delete this chat?" dialog past each tab (the
  // dialog-storm the spec warns against), and exactly one dialog is ever open.
  const [closeQueue, setCloseQueue] = useState<string[]>([]);
  // The delete dialog's 3-way memory choice (#4053/#3493): save a summary, save nothing, or
  // erase. The pendingClose effect (below) re-initialises this from the target tab's incognito
  // flag each time the dialog opens, so an incognito chat starts on "Don't save anything" (and
  // renders no "Save a summary" option).
  const [deleteMemory, setDeleteMemory] = useState<ChatMemoryChoice>(() => defaultMemoryChoice(false));
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
  // "Continue in Zed" (tab menu) — offered only while the operator's external editor is Zed:
  // the hand-off is claimed by the Zed ACP shim, nothing else reads it.
  const editorPref = useEditorPref();
  const toast = useToast();
  const { data: runtimeInfo } = useQuery(runtimeStatusQuery());
  const agentDisplayName = brandName(runtimeInfo?.identity?.name);

  // One shared polite live region for the Conversations tab (§7). It carries two narrow notices:
  // the tab's FIRST appearance (once ever, §3.8), and running-count changes — the latter only
  // while the tab is selected and at most once every 10s. Nothing is announced about running
  // threads while a chat is selected; the tab's accessible name carries the status instead.
  const [convAnnounce, setConvAnnounce] = useState("");
  const announcedAppearRef = useRef(false);
  const lastRunningRef = useRef<number | null>(null);
  const lastRunningAnnounceRef = useRef(0);
  useEffect(() => {
    if (announcedAppearRef.current) return;
    if (!conv.tabVisible || conv.count <= 0) return;
    announcedAppearRef.current = true;
    const origin = conv.rows[0]?.origin_label.replace(/^From /i, "") || "another agent";
    setConvAnnounce(
      `From others tab added: ${conv.count} thread${conv.count === 1 ? "" : "s"} from ${origin}.`,
    );
  }, [conv.tabVisible, conv.count, conv.rows]);
  useEffect(() => {
    // Baseline (no announce) on entry; reset when a chat is selected so re-entering never
    // announces a stale delta.
    if (conv.stage !== "conversations") {
      lastRunningRef.current = null;
      return;
    }
    if (lastRunningRef.current === null) {
      lastRunningRef.current = conv.running;
      return;
    }
    if (lastRunningRef.current === conv.running) return;
    lastRunningRef.current = conv.running;
    const now = Date.now();
    if (now - lastRunningAnnounceRef.current < 10_000) return;
    lastRunningAnnounceRef.current = now;
    setConvAnnounce(`${conv.running} running.`);
  }, [conv.stage, conv.running]);

  // Desktop only: the pinned tab lives in the tab strip (suppressed on phones — C5 puts a
  // Conversations item in the session sheet instead). While it is the selected stage, the list
  // pane replaces the chat session pool in the main pane.
  const onConversationsStage = !mobile && conv.tabVisible && conv.stage === "conversations";

  // The pinned first tab (§3.1). It is drawn only once the list has loaded rows (the store's
  // `tabVisible` owns that rule — not while loading, with zero rows, or after Hide unless a
  // thread needs you). No ✕ / rename / drag (DS `pinned`), an icon not a status dot so it reads
  // as a place, and a non-pulsing StatusDot: `warning` when anything needs you, else `info` when
  // anything is running. The badge pill shows the count; the accessible name spells out the count,
  // the status in words and "read-only" via a visually-hidden suffix (the icon slot is aria-hidden,
  // so the dot can't carry it). The visible number is aria-hidden so it isn't read twice.
  const conversationsTabItem: TabBarItem | null = conv.tabVisible
    ? {
        id: CONVERSATIONS_STAGE_ID,
        pinned: true,
        label: "From others",
        icon: (
          <span className="conversations-tab__icon">
            <MessagesSquare size={14} aria-hidden />
            {(conv.running > 0 || conv.needsYou > 0) && (
              <StatusDot status={conv.needsYou > 0 ? "warning" : "info"} />
            )}
          </span>
        ),
        badge: (
          <>
            <span aria-hidden="true">{conv.count}</span>
            <span className="conversations-tab__sr">
              {`, ${conv.count} thread${conv.count === 1 ? "" : "s"}, ${conv.running} running${
                conv.needsYou > 0 ? `, ${conv.needsYou} needs you` : ""
              }, read-only`}
            </span>
          </>
        ),
      }
    : null;

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

  // Every "new chat" gesture routes through here (§3.3/§4.7). `createSession` only moves the chat
  // store's `currentSessionId`; the Conversations stage lives in the SEPARATE conversations store,
  // which doesn't watch chat-store session changes — so creating a chat while the Conversations tab
  // is selected would switch `currentSessionId` but leave the stage on `conversations`, keeping the
  // new chat hidden behind the read-only list pane (the pool stays display:none). Leaving the stage
  // first means the chat the operator just asked for is actually shown.
  function createChatSession(opts?: { incognito?: boolean }) {
    leaveConversationsForChat();
    chatStore.createSession(opts);
  }

  useEffect(() => {
    if (!chat.currentSessionId && chat.sessions.length === 0) {
      chatStore.createSession();
    }
  }, [chat.currentSessionId, chat.sessions.length]);

  // Deletes asked for outside the tab strip (the mobile SessionSheet, #2512) arrive as a
  // store-level request and use the same close handling — harvest opt-in, server
  // purge, goal Stop-vs-Detach and all. Consumed only while no dialog is open, so a request
  // landing mid-bulk-close waits its turn instead of clobbering the queue. A stale id (the
  // session vanished, e.g. deleted from another tab) is dropped without a dialog.
  useEffect(() => {
    const requested = chat.pendingDeleteRequest;
    if (!requested || pendingClose !== null) return;
    chatStore.clearDeleteRequest();
    requestClose(requested);
  }, [chat.pendingDeleteRequest, pendingClose, chat.sessions]);

  // Re-initialise the delete dialog's memory choice from the TARGET tab's incognito flag
  // whenever the dialog opens or promotes the next queued tab (#4053) — "Save a summary" for an
  // ordinary chat, "Don't save anything" for incognito. Keyed on `pendingClose` alone, reading
  // the incognito flag from the store SNAPSHOT (not reactive `chat.sessions`) so a mid-dialog
  // sessions update can't re-fire and clobber a pick the operator just made. This is the single
  // reset point for the choice; the close helpers no longer hard-reset it.
  useEffect(() => {
    if (pendingClose === null) return;
    const incognito = chatStore.getSnapshot().sessions.find((s) => s.id === pendingClose)?.incognito;
    setDeleteMemory(defaultMemoryChoice(incognito));
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

  function canCloseEmptyChat(id: string): boolean {
    const snapshot = chatStore.getSnapshot();
    const session = snapshot.sessions.find((candidate) => candidate.id === id);
    return !!session && isEmptyChatSession(session)
      && snapshot.sessionStatusMap[id] !== "streaming"
      && serverTurnLabel(id) === null
      && !backgroundSessions.has(id);
  }

  function requestClose(id: string) {
    if (!chatStore.getSnapshot().sessions.some((session) => session.id === id)) return;
    if (canCloseEmptyChat(id)) void closeAfterGoalCheck(id, true);
    else setPendingClose(id);
  }

  // Both no-confirm gestures require a fresh ownership read; stale cached absence
  // must never authorize deleting a goal's session.
  async function closeAfterGoalCheck(id: string, emptyOnly = false) {
    const disposition = await resolveGoalCloseDisposition(id, async () => {
      const result = await goalsState.refetch();
      // Failed background refetches may retain stale successful data.
      return result.error === null && result.data ? result.data.goals : undefined;
    });
    if (!chatStore.getSnapshot().sessions.some((session) => session.id === id)) return;
    if (disposition === "blocked") {
      onError("Couldn't verify whether this chat owns an active goal. The tab was kept; try again.");
    } else if (disposition === "confirm-goal" || (emptyOnly && !canCloseEmptyChat(id))) {
      // A message or turn may have arrived while ownership was being checked.
      setPendingClose(id);
    } else {
      // No confirm, harvest, or forget — including the existing Shift+click contract.
      await closeSession(id, NO_MEMORY_CHANGE);
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
  // (sessionsToClose, anchor excluded). Split it: plain tabs close immediately; goal-driving
  // tabs, whose Stop-vs-Detach choice can't be defaulted safely, are queued through the SAME
  // single-tab confirm one at a time. The pendingClose effect initialises the promoted tab's
  // switches. Bulk close saves NOTHING by default (NO_MEMORY_CHANGE) — it's a cleanup gesture,
  // like Shift+click quick-delete — unless the operator opts in via Settings ▸ Chat ▸ "Save
  // summaries when closing tabs in bulk"; even then an incognito tab is never harvested
  // (defaultMemoryChoice enforces that). The pref is read here at call time, no hook needed.
  function startBulkClose(ids: string[]) {
    if (ids.length === 0) return;
    const harvest = getBulkCloseHarvest();
    const activeGoalIds = new Set(
      goalSessions.filter((g) => g.status === "active").map((g) => g.session_id),
    );
    const goals = ids.filter((id) => activeGoalIds.has(id));
    for (const id of ids) {
      if (activeGoalIds.has(id)) continue;
      const session = chat.sessions.find((s) => s.id === id);
      void closeSession(id, harvest ? defaultMemoryChoice(session?.incognito) : NO_MEMORY_CHANGE);
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
  // Shift+click also skips confirmation for non-empty chats: intercept the close-button
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
      createChatSession({ incognito: true });
      return;
    }
    const closeBtn = (e.target as HTMLElement).closest(".pl-tabbar__close");
    if (!closeBtn) return;
    const tabEl = closeBtn.closest(".pl-tabbar__tab") as HTMLElement | null;
    if (!tabEl) return;
    // The pinned Conversations tab renders first in the strip but is NOT a chat session, so the
    // DOM tab order is offset by one from `chat.sessions` while it is shown. (It has no ✕, so a
    // close click never lands on it — only the index map needs the offset.)
    const tabs = Array.from((e.currentTarget as HTMLElement).querySelectorAll(".pl-tabbar__tab"));
    const pinnedOffset = conv.tabVisible ? 1 : 0;
    const session = chat.sessions[tabs.indexOf(tabEl) - pinnedOffset];
    if (!session) return;
    e.preventDefault();
    e.stopPropagation(); // beat the DS close button's onClick → no confirm dialog
    void closeAfterGoalCheck(session.id);
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
    createChatSession({ incognito: true });
  }

  // Right-click a chat tab → context menu (ADR 0036). The DS TabBar's `onTabContextMenu`
  // (@protolabsai/ui@0.53.0) hands us the session id directly — no sibling-index DOM sniffing.
  // Rename opens the TabBar's inline editor via a synthetic dblclick on the tab element (the DS
  // exposes no start-rename API); we grab that element off the event, not to recover WHICH tab
  // (the hook gives us the id) but only to fire the editor.
  function onTabContextMenu(id: string, e: ReactMouseEvent) {
    // The pinned Conversations tab is not a chat: its only right-click action is Hide (§3.2),
    // which also returns to the chat the operator was reading.
    if (isConversationsStage(id)) {
      openContextMenu("chat-tab", e, {
        // Desktop labels the shared pinned-tab menu "From others" (§9 C7a); the phone session
        // sheet shares this entry but keeps "Conversations" until C7b renames that surface.
        hideConversationsLabel: "Hide “From others”",
        onHideConversations: () => {
          hideConversationsTab();
          leaveConversationsForChat();
        },
      });
      return;
    }
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
      onNew: () => createChatSession(),
      onNewIncognito: () => createChatSession({ incognito: true }),
      onToggleIncognito: () => chatStore.setSessionIncognito(id, !target?.incognito),
      onRename: () => tabEl?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true })),
      onExport: () => void exportChatToFile(id),
      // Hidden for incognito chats: a Zed thread would continue them WITHOUT the incognito
      // flag (the shim sends ordinary turns), silently writing the chat to memory.
      onContinueInZed: editorPref === "zed" && !target?.incognito ? () => handOffToZed(id) : undefined,
      onClose: () => requestClose(id),
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
      onNew: () => createChatSession(),
      onNewIncognito: () => createChatSession({ incognito: true }),
      // "Show From others" (§9 C7a) — offered only while the tab is hidden AND has rows to show
      // but isn't already drawn (needs-you would already force it back on its own). The label
      // rides alongside; the registration reads it only when onShowConversations is present.
      showConversationsLabel: "Show “From others”",
      onShowConversations:
        conv.hidden && conv.count > 0 && !conv.tabVisible ? () => showConversationsTab() : undefined,
    });
  }

  return (
    <section className="panel stage-panel chat-stage" style={active ? undefined : { display: "none" }} aria-hidden={!active} data-kb-scope="chat">
      {/* DS TabBar (#832): a tab per session (status dot · title · close) + "+".
          Double-click a title to rename (TabBar owns the inline EditableText).
          `responsive` collapses in a narrow panel (container query at 26rem) to a quiet
          ghost trigger that opens a themed DS Menu — radio rows to switch the active
          session plus a "Manage" section to rename/close (ui@0.68.2, protoContent#609),
          NOT the earlier native <select>. The status dot (and the incognito eye-off) now
          ride the `icon` slot on BOTH the collapsed trigger and every radio row, where the
          old <option> could host no markup — so the dot shows collapsed as well as wide. */}
      {/* Suppressed on phones: the `responsive` collapse is still a desktop panel idiom (a
          dropdown switcher inside a narrow column), not the chat-first shell's model. That
          shell puts the session title in its header and opens SessionSheet on tap (MobileShell). */}
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
          // When the Conversations tab is the selected stage it reads as the active tab; the
          // chat tabs read unselected. `currentSessionId` still points at the last chat (§4.7).
          activeId={onConversationsStage ? CONVERSATIONS_STAGE_ID : (chat.currentSessionId ?? "")}
          items={[
            ...(conversationsTabItem ? [conversationsTabItem] : []),
            ...chat.sessions.map((session) => {
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
            }),
          ]}
          onSelect={(id) => {
            // Selecting the Conversations tab only switches the stage (§4.7) — it never touches
            // `currentSessionId`, and it preserves the remembered open thread so returning to the
            // tab reopens it (§3.4), rather than resetting to the list. Selecting any chat tab
            // returns to that chat.
            if (isConversationsStage(id)) {
              setConversationsStage("conversations");
              return;
            }
            leaveConversationsForChat();
            chatStore.switchSession(id);
          }}
          onClose={requestClose}
          onRename={(id, label) => {
            chatStore.renameSession(id, label);
            // ADR 0130 S6: push the rename so the title shows on every device (r3).
            pushRename(id, label);
          }}
          // `next` keeps the pinned Conversations tab first (DS reorderUnpinned); drop its
          // reserved id before persisting so it's never written into the chat session order.
          onReorder={(next) =>
            chatStore.reorderSessions(next.filter((t) => !isConversationsStage(t.id)).map((t) => t.id))
          }
          onAdd={() => createChatSession()}
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

      {/* The chat session pool stays MOUNTED while the Conversations tab is selected (its
          in-flight turns keep streaming, mount contract) — just hidden behind the read-only
          list pane, which takes the main pane's content row in its place. */}
      <div className="chat-session-pool" style={onConversationsStage ? { display: "none" } : undefined}>
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

      {/* The Conversations list pane (§3.3) — read-only, no composer. Nothing new renders inside
          a chat pane (§3.8); this replaces the chat entirely while its tab is selected. */}
      {onConversationsStage && <ConversationsPane agentName={agentDisplayName} />}

      {/* One shared polite live region for the tab's narrow screen-reader notices (§7). Kept
          always mounted and visually hidden so an inserted message is announced, not silent. */}
      <span className="conversations-announce" role="status" aria-live="polite" aria-atomic="true">
        {convAnnounce}
      </span>

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
              // DETACH deliberately keeps the server session/goal running; the TAB closes.
              // ADR 0130 S6: close it server-side so every device hides the tab too (kept
              // retrievable, not deleted) — the list is server-authoritative now.
              void api.resumeGoal(id).catch(() => {});
              pushClose(id);
              chatStore.dismissSession(id);
              advanceClose();
              return;
            }
            setRetiringSessionId(id);
            try {
              if (closingGoal && stopGoalOnClose) await api.clearGoal(id, true);
              // Goal closes never touch memory. Otherwise send the chosen memory option — forced
              // to harvest=false for an incognito chat, which renders no "Save a summary" option (#4053).
              const memory = closingGoal
                ? NO_MEMORY_CHANGE
                : pendingCloseSession?.incognito
                  ? { ...deleteMemory, harvest: false }
                  : deleteMemory;
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
                {`"${pendingCloseSession.title}" will be deleted. This can't be undone.`}
              </p>
              {/* One 3-way memory choice (#4053/#3493): save a summary (default for an ordinary
                  chat), save nothing, or erase. Incognito chats drop the "Save a summary" option
                  for a one-line note and default to "Don't save anything". Shared with the clear
                  dialog. */}
              <ChatMemoryChoices
                value={deleteMemory}
                onChange={setDeleteMemory}
                incognito={pendingCloseSession.incognito}
              />
              {/* Surface the otherwise-undiscoverable quick-delete gesture (#1373). Only on the
                  non-goal branch: Shift+click on a goal tab still opens this goal dialog. */}
              <p className="chat-delete-shift-hint">
                Tip: <Kbd>Shift</Kbd>+click a tab's ✕ to delete instantly — no confirmation, nothing saved.
              </p>
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
