// ADR 0057 — the command-palette chat transport. Bridges the DS chat view's
// `AgentTransport` (a `send(history) => AsyncIterable<string>`) onto the console's
// existing `api.streamChat` — which already POSTs A2A 1.0 `SendStreamingMessage` to
// the slug-routed, bearer-authed `/a2a` of the FOCUSED agent and decodes reply text
// from `artifactUpdate` parts. So the palette chat talks to the real agent with zero
// new transport code.
//
// Session model: an EPHEMERAL context per palette-chat open (a fresh contextId on the
// first turn). The palette chat is a self-contained "quick ask the agent" — it doesn't
// entangle the main chat tabs (which would diverge, since the DS view owns its own
// transcript). It inherits the active tab's MODEL so it talks to the same model.
// (Continuing the active session is a future option if context-continuity is wanted.)
import type { AgentTransport } from "@protolabsai/ui/command-palette";
import { api } from "../lib/api";
import { chatStore } from "../chat/chat-store";

/** Build an `AgentTransport` that streams turns to the focused agent via
 *  `api.streamChat`. `name` is shown in the chat view header + composer placeholder. */
export function makeChatTransport(name: string): AgentTransport {
  // Persists across turns within one open chat; reset on a fresh chat (first user turn).
  let sessionId = "";
  return {
    name,
    async *send(history, { signal }) {
      const userTurns = history.filter((m) => m.role === "user").length;
      if (userTurns <= 1 || !sessionId) {
        sessionId = `palette-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      }
      const text = [...history].reverse().find((m) => m.role === "user")?.content ?? "";
      // Inherit the active tab's per-tab model override so the palette uses the same model.
      const snap = chatStore.getSnapshot();
      const model = snap.sessions.find((s) => s.id === snap.currentSessionId)?.model;

      // Bridge api.streamChat's callbacks → an async iterator of appendable text deltas.
      const queue: string[] = [];
      let done = false;
      let error: Error | null = null;
      let wake: (() => void) | null = null;
      const wakeUp = () => {
        const w = wake;
        wake = null;
        w?.();
      };
      // The DS chat view appends each yielded chunk, but streamChat can emit a REPLACE
      // (append=false, e.g. terminal task text). Track what we've yielded and only push
      // the new tail so the transcript never duplicates.
      let acc = "";
      const push = (s: string) => {
        if (s) {
          queue.push(s);
          wakeUp();
        }
      };

      void api
        .streamChat(
          text,
          sessionId,
          {
            signal,
            onText: (t, append) => {
              if (append) {
                acc += t;
                push(t);
              } else if (t.startsWith(acc)) {
                const delta = t.slice(acc.length);
                acc = t;
                push(delta);
              } else {
                acc = t;
                push(t);
              }
            },
            onFailed: (m) => {
              error = new Error(m || "The agent turn failed.");
              done = true;
              wakeUp();
            },
            onDone: () => {
              done = true;
              wakeUp();
            },
          },
          { model },
        )
        .catch((e: unknown) => {
          error = e instanceof Error ? e : new Error(String(e));
          done = true;
          wakeUp();
        });

      while (!done || queue.length) {
        if (!queue.length) {
          await new Promise<void>((resolve) => {
            wake = resolve;
          });
          continue;
        }
        yield queue.shift() as string;
      }
      if (error) throw error;
    },
  };
}
