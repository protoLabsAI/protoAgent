import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { chatStore } from "../chat/chat-store";
import { onTopic } from "../lib/events";
import { notifyIfHidden } from "../lib/notify";
import type { ChatMessage } from "../lib/types";
import { parseScheduledEvent } from "./scheduledEvent";

// Live delivery of scheduled-task results (#2990) into the chat that CREATED the schedule.
// A scheduled fire runs detached in Activity (or its context target), but if it was set up
// from a chat we stamped that chat as the job's `origin_session`; the server then pushes a
// `scheduler.completed` event carrying the result summary. We surface it here as a
// display-only message in that session's transcript:
//   • first fire        → a full ScheduledReportCard (calendar/clock icon + summary + link).
//   • recurring re-fire  → a compact ScheduledChip (`collapse`) so an hourly check can't
//                          fill the thread with 24 cards/day.
// The message is DISPLAY-ONLY — the backend owns conversation history, and the full turn
// lives in the Activity log; this only makes the outcome visible where it was requested.
// If the origin chat isn't open in this window the event is simply ignored (no ghost
// session), mirroring BackgroundWatch.

const NOTIFIED_KEY = "protoagent.schedwatch.notified"; // sessionStorage — survives soft reloads

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

/** Append a display-only scheduled-result message to a session IF it's open in this window.
 *  Returns false when the chat isn't local (the result still lives in Activity). */
function appendScheduled(sessionId: string, scheduled: NonNullable<ChatMessage["scheduled"]>): boolean {
  const session = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
  if (!session) return false;
  const msg: ChatMessage = {
    id: `sched-${scheduled.jobId}-${scheduled.firedAt}`,
    role: "system",
    // Rendering is driven by `scheduled` (ChatMessageView returns the card/chip before
    // the text path); the content is a non-empty fallback so a store that strips empty
    // messages can't drop the card.
    content: `Scheduled task ${scheduled.jobId} ran`,
    createdAt: Date.now(),
    status: "done",
    scheduled,
  };
  chatStore.updateMessages(sessionId, [...session.messages, msg]);
  return true;
}

export function ScheduledWatch() {
  const toast = useToast();

  useEffect(() => {
    return onTopic("scheduler.completed", (data) => {
      const parsed = parseScheduledEvent(data);
      if (!parsed) return;
      const { session, scheduled } = parsed;
      // De-dupe per fire — the replay ring can re-deliver an event on reconnect.
      const key = `${scheduled.jobId}:${scheduled.firedAt}`;
      if (notifiedSet().has(key)) return;
      markNotified(key);
      const failed = scheduled.status === "failed";
      const injected = appendScheduled(session, scheduled);
      // Only toast on the window that owns the origin chat — avoids fleet-wide spam. A
      // recurring re-fire (collapse) is quiet: the chip is enough, no toast/OS notification.
      if (injected && !scheduled.collapse) {
        toast({
          tone: failed ? "error" : "success",
          title: failed ? "Scheduled task failed" : "Scheduled task ran",
          message: `Job ${scheduled.jobId}`,
        });
        notifyIfHidden(failed ? "Scheduled task failed" : "Scheduled task ran", `Job ${scheduled.jobId}`);
      }
    });
  }, [toast]);

  return null;
}
