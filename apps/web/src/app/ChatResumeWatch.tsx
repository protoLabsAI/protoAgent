import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { chatStore } from "../chat/chat-store";
import { onTopic } from "../lib/events";
import { notifyIfHidden } from "../lib/notify";
import type { ChatMessage } from "../lib/types";
import { resumedTurnRender } from "./resumedTurn";

// Live surfacing of a `wait` / scheduled RESUME (ADR 0053, bd-k02) into the chat tab.
// A `wait` yields and is re-triggered server-side by the scheduler, which fires a fresh
// A2A turn into the SAME chat session — but the browser only renders turns it streamed,
// so the resumed turn is invisible until the user next interacts. The server pushes
// `chat.resumed` {session_id, text, task_id, state, error}; we append the resumed answer
// to that session as a normal assistant message (DISPLAY-ONLY — the backend owns
// conversation history, so this never double-feeds the model) and toast. Dedup by task_id
// so an EventSource replay (ADR 0039 ring buffer) on reconnect is idempotent.
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
      if (!target) return; // chat not open in this window — nothing to surface here

      const msg: ChatMessage = {
        id: `resume-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: "assistant",
        content: render.content,
        createdAt: Date.now(),
        status: render.status,
        taskId: render.taskId || undefined,
      };
      chatStore.updateMessages(render.session, [...target.messages, msg]);
      toast(render.toast);
      notifyIfHidden(render.notify.title, render.notify.body);
    });
  }, [toast]);

  return null;
}
