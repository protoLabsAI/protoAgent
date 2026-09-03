import { useEffect } from "react";
import { useSyncExternalStore } from "react";

import { chatStore } from "../chat/chat-store";
import { setAttendedSession } from "../chat/attendSession";

// Session attendance (#3110). Holds an SSE presence stream open to the server for the chat
// session currently on screen (`currentSessionId`), so a completed background job push-resuming
// into that session (ADR 0070) can PARK for `ask_human` / `request_user_input` — the operator is
// watching and can answer — instead of taking the unattended autonomous auto-answer. Mounted once,
// app-wide (alongside ChatResumeWatch); the actual open/close/reconnect lives in attendSession.ts.
//
// The effect re-points attendance whenever the on-screen session changes, and releases it on
// unmount so a closed console (or an operator with no session selected) reads as unattended.
export function ChatAttendance() {
  const currentSessionId = useSyncExternalStore(
    chatStore.subscribe,
    () => chatStore.getSnapshot().currentSessionId,
    () => null,
  );

  useEffect(() => {
    setAttendedSession(currentSessionId);
    return () => setAttendedSession(null);
  }, [currentSessionId]);

  return null;
}
