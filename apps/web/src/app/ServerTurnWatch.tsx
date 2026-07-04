import { useEffect } from "react";

import { labelForOrigin, noteTurnFinished, noteTurnStarted } from "../chat/server-turn-store";
import { onTopic } from "../lib/events";

// Bridges the #1767 `turn.started` / `turn.finished` bus events into the server-turn store,
// so ChatSurface can show its typing indicator during a server-initiated turn (background
// push-resume, a scheduled fire, or a watch reaction). These turns run by self-POSTing into
// a session and hold the connection open for the whole turn — the browser never streams
// them, so without this the app looks hung during its longest turns.
//
// Display-only: it never touches conversation history (the backend owns that; the real
// answer arrives separately via `chat.resumed`, handled by ChatResumeWatch). Mounted once,
// app-wide, alongside the other bus watchers.

export function ServerTurnWatch() {
  useEffect(() => {
    const offStarted = onTopic("turn.started", (data) => {
      const session = String(data.session_id ?? "");
      const origin = String(data.origin ?? "");
      if (session) noteTurnStarted(session, labelForOrigin(origin));
    });
    const offFinished = onTopic("turn.finished", (data) => {
      const session = String(data.session_id ?? "");
      if (session) noteTurnFinished(session);
    });
    return () => {
      offStarted();
      offFinished();
    };
  }, []);

  return null;
}
