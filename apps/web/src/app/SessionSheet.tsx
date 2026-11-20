import { EyeOff, MessagesSquare, Pin, Plus, X } from "lucide-react";
import { useLayoutEffect, useRef, type RefObject } from "react";

import { StatusDot } from "@protolabsai/ui/data";
import { Drawer } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";

import { chatStore, unusedSession, useChatState } from "../chat/chat-store";
import {
  hide as hideConversationsTab,
  openChat as leaveConversationsForChat,
  setStage as setConversationsStage,
  useConversations,
} from "../chat/conversations-store";
import { homeSessionId, inTabOrder } from "../chat/homeTab";
import { useContextMenuStore } from "../contextMenu";

// The mobile session switcher. Replaces the DS TabBar's `responsive` <select> collapse,
// which is a desktop idiom wearing a phone's clothes — a native chat app switches threads
// through a sheet, not a form control.
//
// A DS `Drawer side="bottom"` (protoContent#471, @protolabsai/ui ^0.62) — the sheet comes
// up from the bottom, under the thumb. The Drawer owns the sheet chrome, the scrim/overlay
// dismiss, Esc, the focus trap and the <body> portal, so this component is just the sheet's
// content now (mirrors AppDrawer's swap onto the DS Drawer).
export function SessionSheet({ open, onClose, triggerRef }: {
  open: boolean;
  onClose: () => void;
  triggerRef?: RefObject<HTMLButtonElement | null>;
}) {
  const chat = useChatState();
  const blank = unusedSession(chat);
  // The pinned Home tab (H-U1): listed first, as on the desktop strip, and never deletable.
  // On the phone it is its own row at the very top of the sheet, above "From others", with a
  // pin marker (H-U6); the chat rows below are every other session in their usual order.
  const homeId = homeSessionId();
  const ordered = inTabOrder(chat.sessions, homeId);
  const home = homeId ? chat.sessions.find((session) => session.id === homeId) : undefined;
  const chats = home ? ordered.filter((session) => session !== home) : ordered;
  // The phone version of the pinned Conversations tab (docs/design/other-conversations.md §3.7):
  // a distinct item at the top of the sheet, under the same C1 store that drives the desktop tab.
  const conv = useConversations();

  // Long-press opens the same single-entry Hide “From others” menu the desktop tab shows on
  // right-click (§3.2) — routed through the app-root ContextMenuRenderer, so the DS Menu renders
  // over the sheet. A press that becomes a tap (released, moved into a scroll, or cancelled before
  // the timer fires) clears it; a long-press sets `longPressed` so the trailing click is swallowed.
  const pressTimer = useRef<number | null>(null);
  const longPressed = useRef(false);
  const clearPress = () => {
    if (pressTimer.current != null) {
      clearTimeout(pressTimer.current);
      pressTimer.current = null;
    }
  };
  const openConversationsMenu = (x: number, y: number) => {
    useContextMenuStore.getState().openMenu("chat-tab", x, y, {
      onHideConversations: () => {
        hideConversationsTab();
        leaveConversationsForChat();
      },
    });
  };

  // DS Drawer 0.62 traps focus on open but never returns it to the opener on close, and it
  // exposes no panel ref to hook that from the outside — so the consumer restores focus.
  // Prefer the explicit trigger: Safari does not necessarily focus tapped buttons.
  // Keep the active-element fallback for callers without a trigger ref, capturing it
  // before the Drawer's passive focus-trap effect moves focus into the panel.
  const openerRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (open) {
      openerRef.current = triggerRef?.current ?? document.activeElement as HTMLElement | null;
    } else if (openerRef.current) {
      // Prefer the trigger as it is NOW: picking Home (or leaving it) re-mounts the header
      // title inside/outside its heading, so the element captured on open may be gone.
      const live = triggerRef?.current;
      const opener = live?.isConnected ? live : openerRef.current;
      opener.focus();
      openerRef.current = null;
      // The Drawer dismisses its backdrop on mousedown. The browser's default
      // focus action can then blur the restored trigger; repair that after the event.
      // Do not steal focus from a follow-up dialog (for example, Delete chat).
      const frame = requestAnimationFrame(() => {
        if (document.activeElement === document.body && opener.isConnected) opener.focus();
      });
      return () => cancelAnimationFrame(frame);
    }
  }, [open, triggerRef]);

  const sessionRow = (s: (typeof chat.sessions)[number]) => {
    const status = chat.sessionStatusMap[s.id] || "idle";
    const current = s.id === chat.currentSessionId;
    return (
      <li key={s.id} className={`session-sheet-row${current ? " is-current" : ""}`}>
        <Button
          type="button"
          variant="ghost"
          className="session-sheet-pick"
          aria-current={current || undefined}
          onClick={() => {
            leaveConversationsForChat();
            chatStore.switchSession(s.id);
            onClose();
          }}
        >
          <span className={`session-dot ${status}`} aria-hidden />
          <span className="session-sheet-title">{s.title}</span>
          {s.id === homeId ? <Pin size={13} className="session-sheet-pin" aria-label="pinned" /> : null}
          {s.incognito ? <EyeOff size={13} aria-label="incognito" /> : null}
        </Button>
        {/* Only offer delete while more than one session exists — deleting the last
            one leaves the chat surface with no session to fall back to. Deleting
            goes THROUGH the store request so ChatSurface's confirm dialog (harvest
            opt-in, server purge, goal Stop-vs-Detach) runs — never a direct local
            deleteSession, which skipped all of that (#2512). The sheet closes first
            so the dialog isn't buried under it. */}
        {chat.sessions.length > 1 && s.id !== homeId ? (
          <Button
            type="button"
            icon
            variant="ghost"
            className="session-sheet-del"
            aria-label={`Delete ${s.title}`}
            onClick={() => {
              chatStore.requestDeleteSession(s.id);
              onClose();
            }}
          >
            <X size={15} aria-hidden />
          </Button>
        ) : null}
      </li>
    );
  };

  return (
    <Drawer open={open} onClose={onClose} side="bottom" title="Chats">
      <div className="session-sheet-head">
        {/* Unlike the header "+", this stays enabled whenever a blank exists anywhere:
            from the sheet, landing on that blank IS the useful outcome. It only goes
            dead when the blank is already the current chat. */}
        <Button
          type="button"
          size="sm"
          className="session-sheet-new"
          disabled={blank != null && blank.id === chat.currentSessionId}
          onClick={() => {
            leaveConversationsForChat();
            chatStore.createSession();
            onClose();
          }}
        >
          <Plus size={16} aria-hidden /> New
        </Button>
      </div>
      {/* Home (H-U6): the first row of the sheet, pinned above "From others". Its own one-row
          list so it stays put while the chat rows scroll. */}
      {home ? <ul className="session-sheet-list session-sheet-pinned">{sessionRow(home)}</ul> : null}
      {home && !conv.tabVisible ? (
        <div className="session-sheet-divider" role="separator" aria-hidden="true" />
      ) : null}
      {/* The Conversations item (§3.7) — same icon, label, count and status dot as the desktop
          tab, plus muted "Read-only". No delete ✕. It appears and hides under the store's
          `tabVisible` rules (§3.1/§3.2). Tapping it leaves the sheet for the Conversations stage
          (the remembered open thread, if any, reopens — §3.4); the chat `currentSessionId` is
          never touched (§4.7). */}
      {conv.tabVisible ? (
        <>
          <Button
            type="button"
            variant="ghost"
            className="session-sheet-convo"
            aria-label={`From others, ${conv.count} conversation${conv.count === 1 ? "" : "s"}, ${
              conv.running
            } running${conv.needsYou > 0 ? `, ${conv.needsYou} needs you` : ""}, read-only`}
            onPointerDown={(e) => {
              longPressed.current = false;
              const { clientX, clientY } = e;
              clearPress();
              pressTimer.current = window.setTimeout(() => {
                longPressed.current = true;
                openConversationsMenu(clientX, clientY);
              }, 500);
            }}
            onPointerUp={clearPress}
            onPointerCancel={clearPress}
            onPointerLeave={clearPress}
            onContextMenu={(e) => {
              e.preventDefault();
              clearPress();
              openConversationsMenu(e.clientX, e.clientY);
            }}
            onClick={() => {
              // Swallow the click that follows a long-press (the menu already opened).
              if (longPressed.current) {
                longPressed.current = false;
                return;
              }
              setConversationsStage("conversations");
              onClose();
            }}
          >
            <MessagesSquare size={16} aria-hidden />
            <span className="session-sheet-convo-title">From others</span>
            <span className="session-sheet-convo-count" aria-hidden="true">
              {conv.count}
            </span>
            {/* Matches the tab's dot: warning for Needs you, else info for Running, and never a
                pulse (the pulse lives on the rows inside, not the tab itself, §3.1). */}
            {conv.running > 0 || conv.needsYou > 0 ? (
              <StatusDot status={conv.needsYou > 0 ? "warning" : "info"} pulse={false} />
            ) : null}
            <span className="session-sheet-convo-ro" aria-hidden="true">
              Read-only
            </span>
          </Button>
          <div className="session-sheet-divider" role="separator" aria-hidden="true" />
        </>
      ) : null}
      <ul className="session-sheet-list">{chats.map(sessionRow)}</ul>
    </Drawer>
  );
}
