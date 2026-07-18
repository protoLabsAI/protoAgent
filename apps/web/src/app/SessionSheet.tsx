import { EyeOff, Plus, X } from "lucide-react";
import { useEffect, useRef } from "react";

import { chatStore, useChatState } from "../chat/chat-store";

// The mobile session switcher. Replaces the DS TabBar's `responsive` <select> collapse,
// which is a desktop idiom wearing a phone's clothes — a native chat app switches threads
// through a sheet, not a form control.
//
// Hand-rolled rather than a DS `Drawer` because the DS only supports `side: "left" | "right"`
// (overlays.tsx) and a session switcher wants to come up from the bottom, under the thumb.
// Filed as a DS gap; this mirrors AppDrawer's structure so it's a drop-in swap later.
export function SessionSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const chat = useChatState();
  const sheetRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Swipe-down-to-dismiss. Touch-only and deliberately crude — a drag past 60px closes,
  // anything less snaps back. Enough to feel native without pulling in a gesture library.
  useEffect(() => {
    if (!open) return;
    const el = sheetRef.current;
    if (!el) return;
    let startY = 0;
    let dy = 0;
    const onStart = (e: TouchEvent) => {
      startY = e.touches[0].clientY;
      dy = 0;
      el.style.transition = "none";
    };
    const onMove = (e: TouchEvent) => {
      dy = Math.max(0, e.touches[0].clientY - startY);
      el.style.transform = `translateY(${dy}px)`;
    };
    const onEnd = () => {
      el.style.transition = "";
      el.style.transform = "";
      if (dy > 60) onClose();
    };
    el.addEventListener("touchstart", onStart, { passive: true });
    el.addEventListener("touchmove", onMove, { passive: true });
    el.addEventListener("touchend", onEnd);
    return () => {
      el.removeEventListener("touchstart", onStart);
      el.removeEventListener("touchmove", onMove);
      el.removeEventListener("touchend", onEnd);
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="session-sheet-root" role="dialog" aria-modal="true" aria-label="Chat sessions">
      <div className="session-sheet-backdrop" onClick={onClose} />
      <div className="session-sheet" ref={sheetRef}>
        <div className="session-sheet-grip" aria-hidden />
        <div className="session-sheet-head">
          <h2>Chats</h2>
          <button
            type="button"
            className="session-sheet-new"
            onClick={() => {
              chatStore.createSession();
              onClose();
            }}
          >
            <Plus size={16} aria-hidden /> New
          </button>
        </div>
        <ul className="session-sheet-list">
          {chat.sessions.map((s) => {
            const status = chat.sessionStatusMap[s.id] || "idle";
            const current = s.id === chat.currentSessionId;
            return (
              <li key={s.id} className={`session-sheet-row${current ? " is-current" : ""}`}>
                <button
                  type="button"
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
                </button>
                {/* Only offer delete while more than one session exists — deleting the last
                    one leaves the chat surface with no session to fall back to. */}
                {chat.sessions.length > 1 ? (
                  <button
                    type="button"
                    className="session-sheet-del"
                    aria-label={`Delete ${s.title}`}
                    onClick={() => chatStore.deleteSession(s.id)}
                  >
                    <X size={15} aria-hidden />
                  </button>
                ) : null}
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
