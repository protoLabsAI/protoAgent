import { ChevronDown, ChevronLeft, Menu, Plus } from "lucide-react";
import { useEffect, type ReactNode } from "react";

import { chatStore, unusedSession, useChatState } from "../chat/chat-store";
import { SessionSheet } from "./SessionSheet";

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
  sessionSheetOpen: boolean;
  onSessionSheetChange: (open: boolean) => void;
  /** Runtime warning / agent-down strips, under the header. */
  banners?: ReactNode;
}) {
  const chatState = useChatState();
  const current = chatState.sessions.find((s) => s.id === chatState.currentSessionId);
  // "+" is a no-op when the blank it would reuse is already what you're looking at, so
  // disable it rather than let it read as a dead tap. If the blank is on ANOTHER tab the
  // button stays live — pressing it switches you there, which is a visible result.
  const blank = unusedSession(chatState);
  const newChatIsNoop = blank != null && blank.id === chatState.currentSessionId;

  // Android + installed-PWA hardware back should pop the pushed view rather than exit the
  // app. Push a history entry whenever we leave the chat root and pop it on back, so the
  // system gesture and the header chevron run the same dismissal path.
  useEffect(() => {
    if (!showBack) return;
    window.history.pushState({ mobilePush: true }, "");
    const onPop = () => onBack();
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [showBack, onBack]);

  return (
    <div className="mshell">
      <header className="mshell-head">
        {showBack ? (
          <>
            <button type="button" className="mshell-head-btn" aria-label="Back" onClick={onBack}>
              <ChevronLeft size={22} aria-hidden />
            </button>
            <span className="mshell-title mshell-title--static">
              <span className="mshell-title-text">{title}</span>
            </span>
            {/* Optically centres the title against the back chevron. */}
            <span className="mshell-head-btn" aria-hidden />
          </>
        ) : (
          <>
            <button
              type="button"
              className="mshell-head-btn"
              aria-label="Menu"
              // Same hook as the desktop HamburgerMenu — one selector opens the drawer in
              // either shell, so drawer specs don't fork per breakpoint.
              data-testid="header-menu"
              onClick={onOpenDrawer}
            >
              <Menu size={20} aria-hidden />
            </button>
            {/* The session title IS the switcher — tapping it opens the sheet. Replaces the
                DS TabBar's `responsive` <select>, which ChatSurface suppresses on mobile. */}
            <button
              type="button"
              className="mshell-title"
              onClick={() => onSessionSheetChange(true)}
              aria-haspopup="dialog"
              aria-expanded={sessionSheetOpen}
            >
              <span className="mshell-title-text">{current?.title ?? "New chat"}</span>
              <ChevronDown size={15} aria-hidden />
            </button>
            <button
              type="button"
              className="mshell-head-btn"
              aria-label="New chat"
              disabled={newChatIsNoop}
              title={newChatIsNoop ? "This chat is already empty" : "New chat"}
              onClick={() => chatStore.createSession()}
            >
              <Plus size={20} aria-hidden />
            </button>
          </>
        )}
      </header>

      {banners}

      <div className="mshell-body">
        {/* `inert` only while something actually covers the root — NOT merely when the back
            affordance shows. A docked background plugin view is active INSIDE the root
            (display-toggled, no pushed layer), and inerting it would kill its interactivity. */}
        <div className="mshell-root" inert={pushed != null}>
          {root}
        </div>

        {pushed != null ? <div className="mshell-pushed">{pushed}</div> : null}
      </div>

      <SessionSheet open={sessionSheetOpen} onClose={() => onSessionSheetChange(false)} />
    </div>
  );
}
