import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { chatStore } from "../chat/chat-store";
import { onTopic } from "../lib/events";
import { notifyIfHidden } from "../lib/notify";
import type { ChatMessage } from "../lib/types";

// Live surfacing of a `wait` / scheduled RESUME (ADR 0053, bd-k02) into the chat tab.
// A `wait` yields and is re-triggered server-side by the scheduler, which fires a fresh
// A2A turn into the SAME chat session — but the browser only renders turns it streamed,
// so the resumed turn is invisible until the user next interacts. The server pushes
// `chat.resumed` {session_id, text, task_id}; we append the resumed answer to that
// session as a normal assistant message (DISPLAY-ONLY — the backend owns conversation
// history, so this never double-feeds the model) and toast. Dedup by task_id so an
// EventSource replay (ADR 0039 ring buffer) on reconnect is idempotent.

const seen = new Set<string>();

export function ChatResumeWatch() {
  const toast = useToast();

  useEffect(() => {
    return onTopic("chat.resumed", (data) => {
      const session = String(data.session_id ?? "");
      const text = String(data.text ?? "");
      const taskId = String(data.task_id ?? "");
      const key = taskId || `${session}:${text.slice(0, 32)}`;
      if (!session || !text || seen.has(key)) return;
      seen.add(key);

      const target = chatStore.getSnapshot().sessions.find((s) => s.id === session);
      if (!target) return; // chat not open in this window — nothing to surface here

      const msg: ChatMessage = {
        id: `resume-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: "assistant",
        content: text,
        createdAt: Date.now(),
        status: "done",
      };
      chatStore.updateMessages(session, [...target.messages, msg]);
      toast({ tone: "info", title: "Task resumed", message: "A waited task picked back up in this chat." });
      notifyIfHidden("Task resumed", text.slice(0, 80));
    });
  }, [toast]);

  return null;
}
