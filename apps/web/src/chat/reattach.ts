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
//      finalize reconciles the ORDERED parts (not just flat `content`) off the
//      authoritative task, so a completed MULTI-PART turn whose trailing prose
//      frame was stranded on the wire still renders its answer below the tool
//      cards instead of stopping at the last card (#3082 sibling).
//   4. A turn PAUSED on the operator (input-required / auth-required): the
//      snapshot replay re-renders the pending HITL form; the session goes idle
//      WITHOUT finalize — the turn isn't over, and stamping the message "done"
//      (or holding the session "streaming" while the poller spins) would leave
//      the form's buttons dead (#3082).
//
// Kept store-only (no component state) so any surface can mount it; HITL and
// transient-status hooks are injected by the caller.

import { api, type TurnStreamHandlers } from "../lib/api";
import type { ChatMessage, HitlPayload } from "../lib/types";
import { chatStore } from "./chat-store";
import { replaceText } from "./parts";
import { applyComponent, applyReasoning, applyText, applyToolEvent, applyUsage } from "./turnReducers";

// Kept in sync with streamWatchdog.ts TERMINAL_RE.
const TERMINAL = /completed|failed|canceled|cancelled|rejected/i;
// PAUSED, not over: the server parked the turn waiting on the operator (a HITL
// form/approval, or an auth grant). The task resumes with the operator's answer,
// so the message keeps its status — only the session un-busies.
const PAUSED = /input.required|auth.required/i;
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

/** Stable dependency key for the session slot's reattach effect. Hydration can
 * fill an already-mounted empty fixed-id tab, so sessionId alone is not enough
 * to trigger the effect when its durable streaming assistant appears later. */
export function reattachKeyForMessages(messages: ChatMessage[] | undefined): string {
  const last = [...(messages ?? [])].reverse().find((message) => message.role === "assistant");
  return last?.status === "streaming" && last.taskId && last.id
    ? `${last.id}:${last.taskId}`
    : "";
}

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
    return {
      ...m,
      content: text || m.content,
      // Reconcile the ORDERED parts against the authoritative full-turn text, not
      // just the flat `content`. ChatMessageView renders a parts-bearing bubble
      // FROM its parts (foldPlan) and only falls back to `content` when there are
      // none — so a completed MULTI-PART turn whose trailing prose frame was
      // stranded on the wire (the resubscribe stream closed after the tool cards
      // but before the answer artifact-update) would otherwise render only the
      // last tool card: the GetTask text landed in `content`, which a parts-bearing
      // message never shows (#3082 sibling). replaceText lands the canonical answer
      // as the trailing text run (dropping any partial preamble run so it isn't
      // doubled); with no parts yet (a bare / history-loaded bubble) the content
      // fallback still renders it. Mirrors the live path's finalizeFromTask.
      parts: text ? replaceText(m.parts, text, m.content) : m.parts,
      status: failed ? "error" : "done",
      toolCalls,
    };
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
    onTaskSnapshot: () => updateMessage(sessionId, assistantId, (m) => {
      if (!m.durableSnapshotFallback) return m;
      return {
        ...m,
        content: "",
        reasoning: undefined,
        components: undefined,
        toolCalls: undefined,
        parts: undefined,
        usage: undefined,
        contextWindow: undefined,
        durableSnapshotFallback: undefined,
      };
    }),
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
      if (PAUSED.test(state)) {
        // Paused on operator input — stop polling NOW (this used to spin the
        // full MAX_POLLS budget holding the session "streaming", which kept the
        // re-rendered HITL form's buttons disabled) and free the composer. No
        // finalize: the turn isn't over, the replay above re-rendered the form.
        chatStore.setSessionStatus(sessionId, "idle");
        return;
      }
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
        if (PAUSED.test(state)) {
          // Waiting on the operator (HITL / auth): un-busy the session so the
          // re-rendered form's buttons work, but DON'T finalize — stamping the
          // message "done" would misrepresent a turn the server still owns.
          // Mirrors the live path, where the stream closing on input-required
          // lands on idle without touching the message.
          chatStore.setSessionStatus(sessionId, "idle");
          return;
        }
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
