import { useQuery } from "@tanstack/react-query";
import { StatusDot } from "@protolabsai/ui/data";
import { Button } from "@protolabsai/ui/primitives";
import { ChevronDown, ChevronLeft, Menu, Plus, Search } from "lucide-react";
import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";

import { chatStore, unusedSession, useChatState } from "../chat/chat-store";
import { ConversationsPane } from "../chat/ConversationsPane";
import {
  backToList as backToConversationsList,
  openChat as leaveConversationsForChat,
  useConversations,
} from "../chat/conversations-store";
import { useKbIntents } from "../keybindings/intents";
import { brandName } from "../lib/brand";
import { runtimeStatusQuery } from "../lib/queries";
import { SessionSheet } from "./SessionSheet";

// The shell owns the action; chat places it above its variable-height composer.
const WorkHandleContext = createContext<{ onOpen: () => void; blocked: boolean } | null>(null);

export function MobileWorkHandle() {
  const work = useContext(WorkHandleContext);
  const start = useRef<{ id: number; x: number; y: number } | null>(null);
  const suppressClick = useRef(false);
  if (!work) return null;
  return (
    <Button
      variant="ghost"
      type="button"
      className="mshell-work-handle"
      aria-label="Open Work"
      disabled={work.blocked}
      onPointerDown={(event) => {
        suppressClick.current = false;
        if (work.blocked || event.button !== 0 || !event.isPrimary) return;
        start.current = { id: event.pointerId, x: event.clientX, y: event.clientY };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerUp={(event) => {
        const origin = start.current;
        if (!origin || origin.id !== event.pointerId) return;
        start.current = null;
        const dx = event.clientX - origin.x;
        const dy = event.clientY - origin.y;
        // Allow normal finger jitter during a tap; suppress clicks after drags,
        // including those that miss the upward-swipe threshold.
        suppressClick.current = Math.hypot(dx, dy) > 8;
        if (!work.blocked && dy <= -48 && Math.abs(dy) > 2 * Math.abs(dx)) {
          event.currentTarget.focus();
          work.onOpen();
        }
      }}
      onPointerCancel={() => { start.current = null; suppressClick.current = true; }}
      onLostPointerCapture={() => { start.current = null; }}
      onClick={(event) => {
        if (work.blocked || (event.detail !== 0 && suppressClick.current)) return;
        event.currentTarget.focus();
        work.onOpen();
      }}
    >
      <svg className="mshell-work-glyph" viewBox="0 0 32 4" aria-hidden="true">
        <path d="M2 2h28" stroke="currentColor" strokeWidth="4" strokeLinecap="round" />
      </svg>
    </Button>
  );
}

// The pinned Conversations tab, full screen on the phone (docs/design/other-conversations.md §3.7).
// Reuses the C2/C3 `ConversationsPane` verbatim — it renders the read-only list, or the
// `ConversationReader` thread when the store holds an open thread id. Kept as its own component so
// the `runtimeStatusQuery` read (for the agent's display name) runs ONLY while the layer is shown:
// `MobileShell.test.tsx` mounts the shell with no QueryClientProvider, and the shell never enters
// this branch there (the store is idle), so `useQuery` is never called off a bare mount.
function MobileConversations() {
  const { data: runtimeInfo } = useQuery(runtimeStatusQuery());
  return <ConversationsPane agentName={brandName(runtimeInfo?.identity?.name)} />;
}

/**
 * The chat-first mobile shell (supersedes ADR 0035 D6).
 *
 * D6 defined mobile as "the same surfaces + store, a different shell" — a responsive
 * collapse of the desktop dual-rail IA. That is why mobile read as a shrunken console:
 * a `<select>` to switch threads, a bottom quick-bar of co-equal surfaces, chat as one
 * tab among several.
 *
 * Here chat IS the app. It is the root view; everything else (Work, Knowledge, Memory,
 * plugin views) is pushed over it and dismissed with a back affordance — the shape every
 * native chat app converged on.
 *
 * This deliberately does NOT use the DS `AppShell`. Its mobile branch is a hard early
 * return gated on `isMobile && mobileItems && onMobileSelect && quickBarIds`
 * (app-shell.tsx), and dropping those props to escape it falls through to the *desktop*
 * tree. The DS cannot express chat-as-root, so on mobile we own the shell outright. The
 * DS shell is untouched and still drives every desktop viewport.
 *
 * ── The streaming-continuity contract (#613) ──
 * `root` holds the chat slot AND every docked background plugin view. It is rendered once
 * and never unmounted — visibility inside it is display-toggled, exactly as the desktop
 * dock does. A conventional push/pop navigator that swapped the root out would tear down
 * `ChatSurface` mid-stream and drop the SSE connection.
 */
export function MobileShell({
  root,
  pushed,
  title,
  showBack,
  onBack,
  onOpenDrawer,
  needsYouCount,
  onOpenWork,
  sheetOpen = false,
  sessionSheetOpen,
  onSessionSheetChange,
  banners,
}: {
  /** Chat + docked background plugin views. Always mounted — see the #613 note above. */
  root: ReactNode;
  /** A regular surface, layered over the root. Null when the root is what's showing. */
  pushed: ReactNode | null;
  /** Session title at the chat root; the surface label otherwise. */
  title: string;
  showBack: boolean;
  onBack: () => void;
  onOpenDrawer: () => void;
  needsYouCount: number;
  onOpenWork: () => void;
  sheetOpen?: boolean;
  sessionSheetOpen: boolean;
  onSessionSheetChange: (open: boolean) => void;
  /** Runtime warning / agent-down strips, under the header. */
  banners?: ReactNode;
}) {
  const chatState = useChatState();
  const sessionSheetTriggerRef = useRef<HTMLButtonElement>(null);
  const current = chatState.sessions.find((s) => s.id === chatState.currentSessionId);
  // The Conversations stage (docs/design/other-conversations.md §3.7). The phone has no tab strip,
  // so the stage lives here instead of in ChatSurface (which suppresses the pane on mobile): while
  // `conversations` is the selected stage AND the tab is visible, the list/thread fills the screen
  // over the chat root, the header reads "Conversations" (list) or shows ← List (a thread), and a
  // pushed surface (if one is open) still wins — it layers on top and owns the back header.
  const conv = useConversations();
  const onConversations = conv.tabVisible && conv.stage === "conversations" && pushed == null;
  const onConversationThread = onConversations && conv.openThreadId != null;
  // "+" is a no-op when the blank it would reuse is already what you're looking at, so
  // disable it rather than let it read as a dead tap. If the blank is on ANOTHER tab the
  // button stays live — pressing it switches you there, which is a visible result.
  const blank = unusedSession(chatState);
  const newChatIsNoop = blank != null && blank.id === chatState.currentSessionId;

  // Callback identity is not navigation: App and the chat store can rerender while
  // a collection owns the current history entry. Keep the listener fresh without
  // rebuilding the shell's history entry on those renders.
  const backAction = useRef(onBack);
  useEffect(() => { backAction.current = onBack; }, [onBack]);
  const wasShowingBack = useRef(showBack);

  // Android + installed-PWA hardware back should pop the pushed view rather than exit the
  // app. Push a history entry whenever we leave the chat root and pop it on back, so the
  // system gesture and the header chevron run the same dismissal path.
  useEffect(() => {
    if (!showBack) {
      wasShowingBack.current = false;
      return;
    }
    // Reuse restored entries on reload/remount (including collection-owned entries),
    // and during StrictMode effect replay. A new departure from chat still pushes.
    if (!wasShowingBack.current || !window.history.state?.mobilePush) {
      window.history.pushState({ mobilePush: true }, "");
    }
    wasShowingBack.current = true;
    const onPop = () => backAction.current();
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [showBack]);

  return (
    <div className="mshell">
      <header className="mshell-head">
        {showBack ? (
          <>
            <Button variant="ghost" size="md" icon type="button" aria-label="Back" onClick={onBack}>
              <ChevronLeft size={22} aria-hidden />
            </Button>
            <span className="mshell-title mshell-title--static">
              <span className="mshell-title-text">{title}</span>
            </span>
            {/* Optically centres the title against the back chevron. */}
            <span className="mshell-head-spacer" aria-hidden />
          </>
        ) : onConversationThread ? (
          <>
            {/* A read thread: the header back reads ← List and returns to the list (§3.7). The
                reader renders its own title/badge band below; the centre stays "Conversations" so
                the top bar keeps naming the tab, like the static back header above. */}
            <Button
              variant="ghost"
              size="md"
              type="button"
              className="mshell-back-list"
              aria-label="Back to list"
              onClick={() => backToConversationsList()}
            >
              <ChevronLeft size={20} aria-hidden /> List
            </Button>
            <span className="mshell-title mshell-title--static">
              <span className="mshell-title-text">Conversations</span>
            </span>
            <span className="mshell-head-spacer" aria-hidden />
          </>
        ) : (
          <>
            <Button
              variant="ghost"
              size="md"
              icon
              type="button"
              className="mshell-menu"
              aria-label={needsYouCount > 0 ? `Menu, ${needsYouCount} ${needsYouCount === 1 ? "needs" : "need"} you` : "Menu"}
              // Same hook as the desktop HamburgerMenu — one selector opens the drawer in
              // either shell, so drawer specs don't fork per breakpoint.
              data-testid="header-menu"
              onClick={onOpenDrawer}
            >
              <Menu size={20} aria-hidden />
              {needsYouCount > 0 && (
                <span className="mshell-menu-dot" aria-hidden="true">
                  <StatusDot status="warning" pulse={false} />
                </span>
              )}
            </Button>
            {/* The session title IS the switcher — tapping it opens the sheet. Replaces the
                DS TabBar's `responsive` <select>, which ChatSurface suppresses on mobile. */}
            <button
              ref={sessionSheetTriggerRef}
              type="button"
              className="mshell-title"
              onClick={() => onSessionSheetChange(true)}
              aria-haspopup="dialog"
              aria-expanded={sessionSheetOpen}
            >
              {/* On the Conversations list the title reads "Conversations" where the chat title
                  would be; the tap still opens the sheet (§3.7). */}
              <span className="mshell-title-text">
                {onConversations ? "Conversations" : (current?.title ?? "New chat")}
              </span>
              <ChevronDown size={15} aria-hidden />
            </button>
            {/* The palette, on the shell that needs it MOST and had it least. A phone has no
                keyboard to press the chord with, so without this button every surface behind
                the drawer, every Settings section, the chat's verbs and live knowledge search
                were unreachable on mobile — the shortcut was the only door, and mobile has no
                shortcuts. Icon-only and no chord: there is nothing to teach here.

                Reaches the intents store directly rather than taking a prop, matching the New
                chat button beside it — this header already owns its own actions, and a prop
                would mean a second edit at the App call site for no gain. */}
            <Button
              variant="ghost"
              size="md"
              icon
              type="button"
              aria-label="Search commands"
              title="Search commands, surfaces and agents"
              onClick={(event) => {
                event.currentTarget.focus();
                useKbIntents.getState().togglePalette();
              }}
            >
              <Search size={20} aria-hidden />
            </Button>
            <Button
              variant="ghost"
              size="md"
              icon
              type="button"
              aria-label="New chat"
              disabled={newChatIsNoop}
              title={newChatIsNoop ? "This chat is already empty" : "New chat"}
              onClick={() => { leaveConversationsForChat(); chatStore.createSession(); }}
            >
              <Plus size={20} aria-hidden />
            </Button>
          </>
        )}
      </header>

      {banners}

      <div className="mshell-body">
        {/* `inert` only while something actually covers the root — NOT merely when the back
            affordance shows. A docked background plugin view is active INSIDE the root
            (display-toggled, no pushed layer), and inerting it would kill its interactivity.
            The Conversations layer covers the root the same way a pushed surface does. */}
        <div className="mshell-root" inert={pushed != null || onConversations}>
          <WorkHandleContext.Provider value={showBack || pushed != null || onConversations ? null : { onOpen: onOpenWork, blocked: sessionSheetOpen || sheetOpen }}>
            {root}
          </WorkHandleContext.Provider>
        </div>

        {/* The read-only Conversations list/thread, full screen over the chat root (§3.7). Only
            when no pushed surface is up — a pushed surface layers on top and owns the shell. */}
        {onConversations ? (
          <div className="mshell-conversations">
            <MobileConversations />
          </div>
        ) : null}

        {pushed != null ? <div className="mshell-pushed">{pushed}</div> : null}
      </div>

      <SessionSheet open={sessionSheetOpen} onClose={() => onSessionSheetChange(false)} triggerRef={sessionSheetTriggerRef} />
    </div>
  );
}
