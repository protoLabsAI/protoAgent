import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { chatStore } from "../chat/chat-store";
import { homeSessionId } from "../chat/homeTab";
import { eventIsReplay, onTopic } from "../lib/events";
import { notifyIfHidden } from "../lib/notify";
import type { ChatMessage } from "../lib/types";
import { parseScheduledEvent } from "./scheduledEvent";
import { readKey, writeKey } from "../lib/storage";

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
//
// HOME is the exception (H-U5, #5545): the server also writes every scheduled result into
// Home's thread as a Home entry (`graph/home_entries.py`, id `home-entry-<notice_id>`), and
// the event carries that `notice_id`. So a fire that lands in Home takes the server entry's
// id — hydration's merge (`sessionHydration.mergeHomeEntries`, matched by id) then REPLACES
// the live card with the stored entry instead of adding a second card below it, and a fire
// whose entry already hydrated is not appended at all. Every other chat keeps the
// display-only `sched-…` card: nothing the server stores there would duplicate it.

const NOTIFIED_KEY = "protoagent.schedwatch.notified"; // sessionStorage — survives soft reloads

function notifiedSet(): Set<string> {
  try {
    return new Set(JSON.parse(readKey("session", NOTIFIED_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function markNotified(key: string) {
  try {
    const s = notifiedSet();
    s.add(key);
    writeKey("session", NOTIFIED_KEY, JSON.stringify([...s].slice(-100)));
  } catch {
    /* best-effort */
  }
}

/** Id of the Home entry the server writes for a notice (`home_entries._message`). */
export const homeEntryId = (noticeId: string) => `home-entry-${noticeId}`;

/** Append a display-only scheduled-result message to a session IF it's open in this window.
 *  Returns false when the chat isn't local (the result still lives in Activity).
 *  `noticeId` is the event's notice id; it only matters when the session is Home, where the
 *  card is keyed by the server-written entry so each fire shows once (see the note above). */
function appendScheduled(
  sessionId: string,
  scheduled: NonNullable<ChatMessage["scheduled"]>,
  noticeId?: string,
): boolean {
  const session = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
  if (!session) return false;
  const entryId = noticeId && sessionId === homeSessionId() ? homeEntryId(noticeId) : undefined;
  // Home already holds this fire's stored entry (it hydrated before the event arrived, or the
  // event was replayed to a window that had not seen it): the result is on screen once.
  if (entryId && session.messages.some((message) => message.id === entryId)) return true;
  const msg: ChatMessage = {
    id: entryId ?? `sched-${scheduled.jobId}-${scheduled.firedAt}`,
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
      const noticeId = typeof data.notice_id === "string" && data.notice_id ? data.notice_id : undefined;
      const injected = appendScheduled(session, scheduled, noticeId);
      // Only toast on the window that owns the origin chat — avoids fleet-wide spam. A
      // recurring re-fire (collapse) is quiet: the chip is enough, no toast/OS notification.
      // A fire replayed to a console that was closed still lands as its card; no alert.
      if (injected && !scheduled.collapse && !eventIsReplay()) {
        toast({
          tone: failed ? "error" : "success",
          title: failed ? "Scheduled task failed" : "Scheduled task ran",
          message: `Job ${scheduled.jobId}`,
        });
        // The server raises a notice with every scheduled card it posts, and the desktop
        // shell shows that one — so this is the browser's copy only (the toast stays).
        notifyIfHidden(
          failed ? "Scheduled task failed" : "Scheduled task ran",
          `Job ${scheduled.jobId}`,
          undefined,
          { notice: true, noticeId },
        );
      }
    });
  }, [toast]);

  return null;
}
