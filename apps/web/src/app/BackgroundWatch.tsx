import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { chatStore } from "../chat/chat-store";
import { onTopic } from "../lib/events";
import { notifyIfHidden } from "../lib/notify";
import type { ChatMessage } from "../lib/types";

// Live delivery of background-subagent updates (ADR 0050) into the chat that spawned
// them. A background job runs detached as its own A2A turn; its completion is already
// drained into the spawning session's NEXT model turn (server side). But if that chat is
// still open, the user shouldn't have to send a message to see the result — so the server
// also pushes `background.started`/`background.completed` on the event bus (scoped to this
// window's agent), and we surface them here:
//   • started   → a toast ("running…").
//   • completed → a `system` message injected into the spawning session's transcript +
//                 a toast (and an OS notification if the tab is hidden).
// The injected message is DISPLAY-ONLY — the backend owns conversation history, so this
// never double-feeds the model (the next-turn drain is the model-facing channel).

const NOTIFIED_KEY = "protoagent.bgwatch.notified"; // sessionStorage — survives soft reloads

function notifiedSet(): Set<string> {
  try {
    return new Set(JSON.parse(sessionStorage.getItem(NOTIFIED_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function markNotified(key: string) {
  try {
    const s = notifiedSet();
    s.add(key);
    sessionStorage.setItem(NOTIFIED_KEY, JSON.stringify([...s].slice(-100)));
  } catch {
    /* best-effort */
  }
}

/** Append a display-only system message to a session IF that session is still open in
 *  this window. Returns false when the chat is gone (the model still learns via drain). */
function appendSystem(sessionId: string, content: string): boolean {
  const session = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
  if (!session) return false;
  const msg: ChatMessage = {
    id: `bg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    role: "system",
    content,
    createdAt: Date.now(),
    status: "done",
  };
  chatStore.updateMessages(sessionId, [...session.messages, msg]);
  return true;
}

export function BackgroundWatch() {
  const toast = useToast();

  useEffect(() => {
    const offStarted = onTopic("background.started", (data) => {
      const jobId = String(data.job_id ?? "");
      const key = `start:${jobId}`;
      if (!jobId || notifiedSet().has(key)) return;
      markNotified(key);
      const desc = String(data.description ?? "background task");
      // Toast only on the window that owns the spawning session — avoids fleet-wide spam.
      const owns = chatStore.getSnapshot().sessions.some((s) => s.id === String(data.origin_session ?? ""));
      if (owns) toast({ tone: "info", title: "Background agent started", message: desc });
    });

    const offCompleted = onTopic("background.completed", (data) => {
      const jobId = String(data.job_id ?? "");
      const session = String(data.origin_session ?? "");
      const key = `done:${jobId}`;
      if (!jobId || notifiedSet().has(key)) return;
      markNotified(key);
      const failed = String(data.status ?? "completed") === "failed";
      const desc = String(data.description ?? "background task");
      const result = String(data.result ?? "");
      const header = failed
        ? `⚠️ Background agent failed — ${desc}`
        : `✅ Background agent finished — ${desc}`;
      const injected = appendSystem(session, result ? `${header}\n\n${result}` : header);
      toast({
        tone: failed ? "error" : "success",
        title: failed ? "Background task failed" : "Background task finished",
        message: injected ? desc : `${desc} — open the chat to read it.`,
      });
      notifyIfHidden(failed ? "Background task failed" : "Background task finished", desc);
    });

    return () => {
      offStarted();
      offCompleted();
    };
  }, [toast]);

  return null;
}
