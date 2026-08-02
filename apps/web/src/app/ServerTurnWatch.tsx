import { useEffect } from "react";

import { chatStore } from "../chat/chat-store";
import { labelForOrigin, noteTurnFinished, noteTurnStarted } from "../chat/server-turn-store";
import { onTopic } from "../lib/events";
import { applyProgressFrame, parseProgress } from "./serverTurnProgress";

// Bridges the #1767 `turn.started` / `turn.finished` bus events into the server-turn store,
// so ChatSurface can show its typing indicator during a server-initiated turn (background
// push-resume, a scheduled fire, or a watch reaction). These turns run by self-POSTing into
// a session and hold the connection open for the whole turn — the browser never streams
// them, so without this the app looks hung during its longest turns.
//
// It also folds `chat.progress` frames (#2361) into a growing assistant bubble, so these
// turns are watchable while they run instead of showing a typing indicator for minutes and
// then the whole answer at once. The backend republishes tool + narration frames for
// server-fired turns only — a turn the browser streams itself is never republished, or
// every card would render twice.
//
// Display-only: the backend owns conversation history, so nothing here is fed back to the
// model. The live bubble is a preview that `chat.resumed` REPLACES with the authoritative
// final answer (ChatResumeWatch matches it by id). Mounted once, app-wide, alongside the
// other bus watchers.

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
    const offProgress = onTopic("chat.progress", (data) => {
      const frame = parseProgress(data);
      if (!frame) return;
      const target = chatStore.getSnapshot().sessions.find((s) => s.id === frame.session);
      if (!target) return; // chat not open in this window — nothing to surface here
      chatStore.updateMessages(frame.session, applyProgressFrame(target.messages, frame));
    });
    return () => {
      offStarted();
      offFinished();
      offProgress();
    };
  }, []);

  return null;
}
