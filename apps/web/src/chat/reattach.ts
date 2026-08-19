// Reattach an interrupted turn (Swap & Resume S1). The operator switched
// agents / reloaded / lost the network mid-turn; the turn is server-owned and
// kept running (pinned by tests/test_a2a_turn_survival.py). On return:
//
//   1. `tasks/resubscribe` — the server replays the durable task snapshot
//      (accumulated text + every tool/reasoning frame emitted while nobody was
//      watching), then streams live frames until the turn completes. The
//      transcript catches up and then follows along, exactly like a live turn.
//   2. A cold agent (still booting behind the fleet proxy) answers 409/502 —
//      retry with backoff instead of giving up (the old self-heal's `catch
//      { return; }` froze the bubble forever in exactly this case).
//   3. A turn that already ENDED while detached: resubscribe is rejected for
//      terminal tasks, so fall back to one GetTask snapshot replay + finalize.
//
// Kept store-only (no component state) so any surface can mount it; HITL and
// transient-status hooks are injected by the caller.

import { api, type TurnStreamHandlers } from "../lib/api";
import type { HitlPayload } from "../lib/types";
import { chatStore } from "./chat-store";
import { applyComponent, applyReasoning, applyText, applyToolEvent, applyUsage } from "./turnReducers";

const TERMINAL = /completed|failed|canceled|cancelled/i;
// Cold-agent / transient-transport signatures worth retrying (mirrors the
// query client's retry policy for member boots).
const COLD = /\b(409|502|503|504)\b|Failed to fetch|NetworkError|Load failed|network/i;
const MAX_ATTEMPTS = 8;
const BACKOFF_MS = [1000, 2000, 3000, 5000, 8000, 10000, 10000, 10000];
// The fallback poller's ceiling — sized to the fleet proxy's per-turn budget
// (600s), not the old 2 minutes that stranded long turns mid-"streaming".
const POLL_INTERVAL_MS = 3000;
const MAX_POLLS = 200;

export type ReattachHooks = {
  onHitl?: (payload: HitlPayload) => void;
  onStatus?: (status: string) => void;
};

function updateMessage(sessionId: string, assistantId: string, fn: (m: any) => any) {
  const cur = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
  if (!cur) return;
  chatStore.updateMessages(
    sessionId,
    cur.messages.map((m) => (m.id === assistantId ? fn(m) : m)),
  );
}

function finalize(sessionId: string, assistantId: string, state: string, text: string) {
  const failed = /fail|cancel/i.test(state);
  updateMessage(sessionId, assistantId, (m) => {
    const toolCalls = m.toolCalls?.map((c: { status: string }) =>
      c.status === "running" ? { ...c, status: "done" as const } : c,
    );
    return { ...m, content: text || m.content, status: failed ? "error" : "done", toolCalls };
  });
  chatStore.setSessionStatus(sessionId, failed ? "error" : "idle");
}

/** Reattach the stuck assistant message to its server-owned task. Returns a
 * cancel function (unmount / a new live turn taking over). */
export function reattachTurn(sessionId: string, assistantId: string, taskId: string, hooks: ReattachHooks = {}) {
  let cancelled = false;
  const controller = new AbortController();

  const handlers: TurnStreamHandlers = {
    signal: controller.signal,
    onStatus: (status) => hooks.onStatus?.(status),
    onText: (text, append) => updateMessage(sessionId, assistantId, (m) => applyText(m, text, append)),
    onReasoning: (delta) => updateMessage(sessionId, assistantId, (m) => applyReasoning(m, delta)),
    onToolCall: (evt) => {
      if (evt.name === "show_component") return; // rendered via onComponent — no card noise
      updateMessage(sessionId, assistantId, (m) => applyToolEvent(m, evt));
    },
    onComponent: (spec) => updateMessage(sessionId, assistantId, (m) => applyComponent(m, spec)),
    onCost: (usage) => updateMessage(sessionId, assistantId, (m) => applyUsage(m, usage)),
    onContext: (contextWindow) => updateMessage(sessionId, assistantId, (m) => ({ ...m, contextWindow })),
    onInputRequired: (payload) => hooks.onHitl?.(payload),
    onFailed: () => {
      /* terminal failure — the post-stream GetTask below finalizes with the error text */
    },
  };

  async function fallbackPoll(): Promise<void> {
    for (let polls = 0; polls < MAX_POLLS && !cancelled; polls++) {
      let state = "";
      let sawTask = true;
      try {
        // replayTask routes the snapshot through the shared dispatcher, so a
        // turn that finished while we were away still lands its tool cards.
        state = await api.replayTask(taskId, sessionId, handlers);
      } catch (err) {
        if (!COLD.test(String(err))) sawTask = false; // gone/rejected — un-stick below
      }
      if (cancelled) return;
      if (!sawTask || !state || TERMINAL.test(state)) {
        const { state: s2, text } = await api.getTask(taskId).catch(() => ({ state: "", text: "" }));
        finalize(sessionId, assistantId, s2 || state, text);
        return;
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
  }

  async function run() {
    chatStore.setSessionStatus(sessionId, "streaming");
    for (let attempt = 0; attempt < MAX_ATTEMPTS && !cancelled; attempt++) {
      try {
        await api.resumeTask(taskId, sessionId, handlers);
        // Stream closed = the turn is over (terminal-by-state, A2A 1.0). Confirm
        // and finalize off the durable task.
        if (cancelled) return;
        const { state, text } = await api.getTask(taskId).catch(() => ({ state: "completed", text: "" }));
        finalize(sessionId, assistantId, state || "completed", text);
        return;
      } catch (err) {
        if (cancelled) return;
        if (COLD.test(String(err))) {
          await new Promise((r) => setTimeout(r, BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)]));
          continue;
        }
        // Not a cold agent: most likely the task already ENDED (resubscribe
        // rejects terminal tasks) — replay the snapshot once and finalize.
        break;
      }
    }
    if (!cancelled) await fallbackPoll();
  }

  void run().catch(() => {
    /* reattach is best-effort — never crash the surface */
  });

  return () => {
    cancelled = true;
    controller.abort();
  };
}
