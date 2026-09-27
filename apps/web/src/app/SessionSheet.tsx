import { EyeOff, Plus, X } from "lucide-react";
import { useLayoutEffect, useRef } from "react";

import { Drawer } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";

import { chatStore, unusedSession, useChatState } from "../chat/chat-store";

// The mobile session switcher. Replaces the DS TabBar's `responsive` <select> collapse,
// which is a desktop idiom wearing a phone's clothes — a native chat app switches threads
// through a sheet, not a form control.
//
// A DS `Drawer side="bottom"` (protoContent#471, @protolabsai/ui ^0.62) — the sheet comes
// up from the bottom, under the thumb. The Drawer owns the sheet chrome, the scrim/overlay
// dismiss, Esc, the focus trap and the <body> portal, so this component is just the sheet's
// content now (mirrors AppDrawer's swap onto the DS Drawer).
export function SessionSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const chat = useChatState();
  const blank = unusedSession(chat);

  // DS Drawer 0.62 traps focus on open but never returns it to the opener on close, and it
  // exposes no panel ref to hook that from the outside — so the consumer restores focus.
  // Capture the focused trigger BEFORE the Drawer's focus-trap moves focus into the panel:
  // this layout effect flushes ahead of the Drawer's passive focus-trap effect. On close,
  // send focus back to the header title button that opened the sheet (MobileShell.tsx).
  const openerRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (open) {
      openerRef.current = document.activeElement as HTMLElement | null;
    } else if (openerRef.current) {
      openerRef.current.focus();
      openerRef.current = null;
    }
  }, [open]);

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
            chatStore.createSession();
            onClose();
          }}
        >
          <Plus size={16} aria-hidden /> New
        </Button>
      </div>
      <ul className="session-sheet-list">
        {chat.sessions.map((s) => {
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
                  chatStore.switchSession(s.id);
                  onClose();
                }}
              >
                <span className={`session-dot ${status}`} aria-hidden />
                <span className="session-sheet-title">{s.title}</span>
                {s.incognito ? <EyeOff size={13} aria-label="incognito" /> : null}
              </Button>
              {/* Only offer delete while more than one session exists — deleting the last
                  one leaves the chat surface with no session to fall back to. Deleting
                  goes THROUGH the store request so ChatSurface's confirm dialog (harvest
                  opt-in, server purge, goal Stop-vs-Detach) runs — never a direct local
                  deleteSession, which skipped all of that (#2512). The sheet closes first
                  so the dialog isn't buried under it. */}
              {chat.sessions.length > 1 ? (
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
        })}
      </ul>
    </Drawer>
  );
}
