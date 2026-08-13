import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { chatStore } from "../chat/chat-store";
import { onTopic } from "../lib/events";
import { notifyIfHidden } from "../lib/notify";
import type { ChatMessage } from "../lib/types";
import { resumedTurnRender } from "./resumedTurn";
import { isLiveServerTurn } from "./serverTurnProgress";

// Live surfacing of a `wait` / scheduled RESUME (ADR 0053, bd-k02) into the chat tab.
// A `wait` yields and is re-triggered server-side by the scheduler, which fires a fresh
// A2A turn into the SAME chat session — but the browser only renders turns it streamed,
// so the resumed turn is invisible until the user next interacts. The server pushes
// `chat.resumed` {session_id, text, task_id, state, error}; we append the resumed answer
// to that session as a normal assistant message (DISPLAY-ONLY — the backend owns
// conversation history, so this never double-feeds the model) and toast. Dedup by task_id
// so an EventSource replay (ADR 0039 ring buffer) on reconnect is idempotent.
//
// When the turn streamed live (#2361), ServerTurnWatch has already grown a preview bubble
// for it. This event carries the AUTHORITATIVE final answer, so it REPLACES that bubble
// in place rather than appending a second one — otherwise the operator ends a turn looking
// at the same content twice, once mid-flight and once complete. Matched by the deterministic
// live-message id (task id, else session), so the replacement can't grab a neighbouring turn.
//
// A FAILED resume renders as a failure, not as an answer. These turns don't stream to the
// browser, so this event is the operator's only live view of them — and a crashed one used
// to arrive as a normal bubble whose text just stopped mid-sentence, which reads as the
// agent finishing rather than dying. The bubble is parked at status "error" (matching
// ChatSurface's own `failed ? "error" : "done"`) with the server's reason appended, and the
// toast is toned to match. A crash before any text produces an error-only bubble — the
// turn is visible either way.

const seen = new Set<string>();

export function ChatResumeWatch() {
  const toast = useToast();

  useEffect(() => {
    return onTopic("chat.resumed", (data) => {
      const render = resumedTurnRender(data);
      if (!render || seen.has(render.key)) return;
      seen.add(render.key);

      const target = chatStore.getSnapshot().sessions.find((s) => s.id === render.session);
      if (!target) {
        // Chat not open in this window — there's no transcript to inject into, but
        // the toast/OS-notification are the only live signal this turn resumed at
        // all (#2692's same-root-cause sibling: unlike BackgroundWatch, this path
        // used to drop the event entirely rather than degrade to a toast).
        toast(render.toast);
        notifyIfHidden(render.notify.title, render.notify.body);
        return;
      }

      const liveIdx = target.messages.findIndex((m) =>
        isLiveServerTurn(m, render.taskId, render.session),
      );
      const live = liveIdx >= 0 ? target.messages[liveIdx] : null;
      const msg: ChatMessage = {
        id: live?.id ?? `resume-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: "assistant",
        content: render.content,
        createdAt: live?.createdAt ?? Date.now(),
        status: render.status,
        taskId: render.taskId || undefined,
        // Keep the tool cards the live view already rendered — the resume payload carries
        // the final TEXT only, so dropping these would erase the turn's visible work.
        // `parts` is deliberately not carried over: it interleaves the streamed text, which
        // the authoritative `content` now supersedes, so the message falls back to the
        // grouped tools→content layout history-loaded messages already use.
        toolCalls: live?.toolCalls,
      };
      const next =
        liveIdx >= 0
          ? target.messages.map((m, i) => (i === liveIdx ? msg : m))
          : [...target.messages, msg];
      chatStore.updateMessages(render.session, next);
      toast(render.toast);
      notifyIfHidden(render.notify.title, render.notify.body);
    });
  }, [toast]);

  return null;
}
