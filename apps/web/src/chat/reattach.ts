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
//      The server does NOT close `SubscribeToTask` for a paused task, and must not:
//      input-required is an INTERRUPTED state, not a terminal one, and the stream
//      ends only at a terminal state (A2A §3.1.6) — the same task continues when
//      the operator answers (§3.4.3). So "stream closed" can never be how a
//      reattach learns a turn is paused. The moment the stream reports a paused
//      state — the opening snapshot, or a live status update when the turn parks
//      while we're attached — the reattach settles as paused and lets go of the
//      stream. Waiting for the close held the session "streaming" (Stop up, the
//      form's Send/Dismiss disabled) for as long as the operator didn't answer
//      the form they couldn't answer (#3930).
//
// Kept store-only (no component state) so any surface can mount it; HITL and
// transient-status hooks are injected by the caller.

import { api, supersededByFromStatus, type DurableChatTurn, type TurnStreamHandlers } from "../lib/api";
import type { ChatMessage, HitlPayload } from "../lib/types";
import { chatStore } from "./chat-store";
import { isLiveServerTurn, serverTurnLabel } from "./server-turn-store";
import { messagesFromDurableTurns } from "./sessionHydration";
import { beginReattach, reconcileSessionStatus } from "./sessionLiveness";
import {
  applyComponent,
  applyReasoning,
  applyText,
  applyToolEvent,
  applyUsage,
  pauseBubble,
} from "./turnReducers";
import { applyCanonicalTurnText, resetTurnForSnapshot, settleTurnBubbles } from "./turnText";

export { pauseBubble, unpauseBubble } from "./turnReducers";

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

// ── one producer per bubble ─────────────────────────────────────────────────────────
//
// A server-fired turn (background push-resume, scheduled fire, watch reaction) has a
// second live producer the browser didn't start: the bus's `chat.progress` frames, which
// ServerTurnWatch folds into a preview bubble (#2361). That preview is `streaming` with a
// `taskId` — exactly what the reattach effect looks for — so once #3178 made the effect
// re-run on `reattachKey`, the preview's first frame triggered a resubscribe to the SAME
// still-running task, and the stream and the bus both wrote every chunk into one bubble.
// The duplication vanished only when the final answer replaced the bubble wholesale.
//
// So exactly one of them drives a given bubble:
//   * a server turn this console is WATCHING LIVE keeps its bus feed — no reattach;
//   * a reattach that does run (a reload or a mid-turn open, where the console never saw
//     the turn start) owns its bubble, and the bus feed stands aside for it.

/** Messages a reattach is currently driving → the token of the reattach that owns each.
 *  A token, not a flag: an earlier reattach finishing late must not release a newer one. */
const driving = new Map<string, symbol>();

/** True while a reattach is streaming into `messageId` — any other producer must stand
 *  aside, or the two write the same chunks into one bubble. */
export function isReattaching(messageId: string): boolean {
  return driving.has(messageId);
}

/** Whether the slot's reattach effect should resubscribe to `last`'s task: a `streaming`
 *  assistant message with a task, EXCEPT a live server-turn preview while this console
 *  is watching that turn — the bus already feeds it, and `chat.resumed` settles it. A
 *  preview whose turn this console did NOT see running (a reload, a mid-turn open) or
 *  one stranded after it ended still reattaches: that is the self-heal it exists for. */
export function shouldReattach(
  last: ChatMessage | undefined,
  sessionId: string,
): last is ChatMessage & { id: string; taskId: string } {
  if (!last || last.status !== "streaming" || !last.taskId || !last.id) return false;
  if (isLiveServerTurn(last, last.taskId, sessionId) && serverTurnLabel(sessionId) !== null) return false;
  return true;
}

/** The lead turn's latest assistant bubble — skipping rows a PARTICIPANT spoke (`author`)
 *  and the lead's outgoing asks to one (`addressedTo`), the #3449 room shape. Those land
 *  as their own already-settled rows AFTER the live preview while the turn is still
 *  running, so "the last assistant row" named one of them and read the turn as over: the
 *  slot's reattach effect cancelled a live reattach, and when the turn really ended there
 *  was no reattach left to release the session — it sat "streaming" for good.
 *
 *  It also skips another turn's result that `chat.resumed` appended with no preview of its
 *  own (`outOfBand`). That row lands after a still-running turn too, and naming it cancelled
 *  that turn's reattach. For an operator turn, which never gets a `chat.resumed` of its
 *  own, nothing then settled its bubble, and the session stayed "streaming". */
export function leadAssistantMessage(messages: ChatMessage[] | undefined): ChatMessage | undefined {
  return [...(messages ?? [])]
    .reverse()
    .find(
      (message) => message.role === "assistant" && !message.author && !message.addressedTo && !message.outOfBand,
    );
}

/** Mark `messageId` PAUSED on the operator (#3946): the turn parked (input-required) and
 *  nothing is producing into the bubble until the operator answers. Its status stays
 *  `streaming` — the turn is not over and the answer continues it — but the bubble and its
 *  in-flight cards render as waiting: before this a fresh browser's reattach left the
 *  `ask_human` card spinning with a climbing timer, and an empty bubble showed the
 *  streaming placeholder, for as long as the form went unanswered. Only a streaming bubble
 *  is touched. */
export function markTurnPaused(messages: ChatMessage[], messageId: string): ChatMessage[] {
  return messages.map((m) =>
    m.id === messageId && m.status === "streaming" ? pauseBubble(m) : m,
  );
}

/** The paused bubble a HITL answer just answered, settled. A reattach that found its turn
 *  PAUSED leaves the bubble `streaming` on purpose — the turn was not over — and the answer
 *  then continues the task in a NEW bubble (a form/question answer), so nothing else would
 *  ever settle the old one: it spun its `ask_human` card and its spinner for good (#3930).
 *  Marks `messageId` done, with any still-running tool card done too (the answer's own
 *  stream closes that card in the new bubble). A bubble that is not streaming is left
 *  alone. */
export function settleAnsweredPause(messages: ChatMessage[], messageId: string | undefined): ChatMessage[] {
  if (!messageId) return messages;
  return messages.map((m) =>
    m.id === messageId && m.status === "streaming"
      ? {
          ...m,
          status: "done",
          paused: undefined,
          durableSnapshotFallback: undefined,
          toolCalls: m.toolCalls?.map((c) =>
            c.status === "running" ? { ...c, status: "done" as const, paused: undefined } : c,
          ),
        }
      : m,
  );
}

/** Stable dependency key for the session slot's reattach effect. Hydration can
 * fill an already-mounted empty fixed-id tab, so sessionId alone is not enough
 * to trigger the effect when its durable streaming assistant appears later. */
export function reattachKeyForMessages(messages: ChatMessage[] | undefined): string {
  const last = leadAssistantMessage(messages);
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
  const cur = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
  if (cur) {
    // Reconcile the ORDERED parts against the authoritative full-turn text, not
    // just the flat `content`. ChatMessageView renders a parts-bearing bubble FROM
    // its parts (foldPlan) and only falls back to `content` when there are none —
    // so a completed MULTI-PART turn whose trailing prose frame was stranded on the
    // wire (the resubscribe stream closed after the tool cards but before the answer
    // artifact-update) would otherwise render only the last tool card: the GetTask
    // text landed in `content`, which a parts-bearing message never shows (#3082
    // sibling). That text is the whole TURN's answer and the turn may span several
    // bubbles (a consumed steer / delegation split it), so it is distributed across
    // them — landing all of it on the live bubble would draw the earlier bubble's
    // prose a second time. Mirrors the live path's finalizeFromTask.
    const reconciled = text ? applyCanonicalTurnText(cur.messages, assistantId, text) : cur.messages;
    chatStore.updateMessages(
      sessionId,
      settleTurnBubbles(
        reconciled.map((m) => {
          if (m.id !== assistantId) return m;
          const toolCalls = m.toolCalls?.map((c) =>
            c.status === "running" ? { ...c, status: "done" as const, paused: undefined } : c,
          );
          return { ...m, status: failed ? "error" : "done", paused: undefined, toolCalls, durableSnapshotFallback: undefined };
        }),
        assistantId,
      ),
    );
  }
  chatStore.setSessionStatus(sessionId, failed ? "error" : "idle");
}

// ── a pause another task took over (#3963) ─────────────────────────────────────────────
//
// A plain message sent while a turn waits on the operator — from another surface, or from a
// console that had lost the form — is held and RE-PARKS the pause on a new task; the old task
// is completed with a pointer to it. A console reattaching to the old task (a warm tab that
// last saw it parked) found a finished turn and settled it: the form never came back, the
// next composer reply was held and re-asked on yet another task, and so on. The reattach now
// follows the pointer: the old bubble settles, and the task(s) that took the pause over are
// drawn after it the way hydration draws them — the live one last, so the slot's reattach
// resubscribes to IT and the form comes back.

/** Hops followed at most: each is one GetTask, and a chain longer than this is not one a
 *  person made by hand. */
const MAX_SUCCESSORS = 5;

/** The tasks that took `firstId`'s pause over, oldest first, following each one's own
 *  pointer. Best-effort: a hop that fails ends the chain there. */
async function successorTurns(firstId: string, cancelled: () => boolean): Promise<DurableChatTurn[]> {
  const turns: DurableChatTurn[] = [];
  const seen = new Set<string>();
  let next: string | undefined = firstId;
  while (next && !seen.has(next) && turns.length < MAX_SUCCESSORS && !cancelled()) {
    seen.add(next);
    const turn: DurableChatTurn | null = await api.getTaskTurn(next).catch(() => null);
    if (!turn) break;
    turns.push(turn);
    next = supersededByFromStatus(turn.status);
  }
  return turns;
}

/** Draw the turns that took a settled pause over after the session's transcript — skipping
 *  any this console already shows (it sent that message itself) — with the live one last. */
export function appendSuccessorTurns(sessionId: string, turns: DurableChatTurn[]) {
  const cur = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
  if (!cur || !turns.length) return;
  const shown = new Set(cur.messages.map((m) => m.taskId).filter(Boolean));
  const fresh = turns.filter((turn) => !shown.has(turn.task_id));
  if (!fresh.length) return;
  chatStore.updateMessages(sessionId, [...cur.messages, ...messagesFromDurableTurns(fresh)]);
}

/** Reattach the stuck assistant message to its server-owned task. Returns a
 * cancel function (unmount / a new live turn taking over). */
export function reattachTurn(sessionId: string, assistantId: string, taskId: string, hooks: ReattachHooks = {}) {
  let cancelled = false;
  const controller = new AbortController();
  const token = Symbol(assistantId);
  driving.set(assistantId, token);
  const release = () => {
    if (driving.get(assistantId) === token) driving.delete(assistantId);
  };
  // While this is registered, the session's "streaming" is a live reattach's and the
  // reconciler leaves it alone (sessionLiveness.ts).
  const endClaim = beginReattach(sessionId);

  /** Let go of the session and hand its "streaming" back if nothing else still holds it.
   *
   *  The slot cancels a reattach whenever its bubble stops being a live one, and the usual
   *  reason is that ANOTHER producer settled it first: the bus's `chat.resumed` replacing a
   *  server turn's preview after a reload, while the resubscribe was still waiting on the
   *  server. run() then never reaches finalize, and before #3474 nothing released the
   *  status it set: Stop stayed up, Send stayed disabled, and any interjection queued for
   *  that turn was held back as if this browser's own stream would drain it. The reconciler
   *  decides. An unmount mid-turn leaves a streaming bubble, so the status stays for the
   *  reattach the next mount starts, and a turn started since keeps its own. */
  function letGo() {
    release();
    endClaim();
    reconcileSessionStatus(sessionId);
  }

  // Set once the subscription reports the task PAUSED on the operator (step 4 above).
  // `detached` then mutes whatever the dropped stream still delivers: the desktop relay
  // cannot be aborted, and an answer continuing this task must not be written into the
  // bubble by a producer that already let go of it.
  let paused = false;
  let detached = false;
  let onPaused: () => void = () => {};
  const pausedSignal = new Promise<"paused">((resolve) => {
    onPaused = () => resolve("paused");
  });

  const handlers: TurnStreamHandlers = {
    signal: controller.signal,
    // Every Task frame is a full authoritative snapshot, not a delta. Reset
    // replay-derived fields before EACH one: a subscription may deliver its
    // snapshot and then fail, after which retry/GetTask replays the same history.
    // If no Task frame arrives this hook never runs, preserving the hydrated
    // durable partial as the cold/failure fallback.
    onTaskSnapshot: () => {
      // A snapshot is authoritative for the WHOLE turn, so a turn the console had
      // split to place a consumed steer / delegation folds back to one bubble first
      // (resetTurnForSnapshot) — the snapshot flattens every text frame into one
      // accumulation and cannot honestly re-derive where the split belonged.
      const cur = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
      if (cur) chatStore.updateMessages(sessionId, resetTurnForSnapshot(cur.messages, assistantId));
      updateMessage(sessionId, assistantId, (m) => ({
        ...m,
        content: "",
        reasoning: undefined,
        components: undefined,
        toolCalls: undefined,
        parts: undefined,
        usage: undefined,
        contextWindow: undefined,
        durableSnapshotFallback: undefined,
        // The snapshot says what state the turn is in now; a pause is re-marked below.
        paused: undefined,
      }));
    },
    onStatus: (status) => hooks.onStatus?.(status),
    onText: (text, append) => {
      if (append) {
        updateMessage(sessionId, assistantId, (m) => applyText(m, text, true));
        return;
      }
      // A replace carries the whole turn — distribute it (turnText.ts).
      const cur = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
      if (cur) chatStore.updateMessages(sessionId, applyCanonicalTurnText(cur.messages, assistantId, text));
    },
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
        const cur = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
        if (cur) chatStore.updateMessages(sessionId, markTurnPaused(cur.messages, assistantId));
        chatStore.setSessionStatus(sessionId, "idle");
        return;
      }
      if (!sawTask || !state || TERMINAL.test(state)) {
        const { state: s2, text, supersededBy } = await api
          .getTask(taskId)
          .catch(() => ({ state: "", text: "", supersededBy: undefined }));
        if (cancelled) return; // a late answer must not settle over a turn started since the cancel
        await settle(s2 || state, text, supersededBy);
        return;
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
  }

  // The subscription's handlers: the shared ones, muted once the stream is let go, plus the
  // paused-state watch.
  const streamHandlers: TurnStreamHandlers = {
    ...(Object.fromEntries(
      Object.entries(handlers).map(([key, value]) => [
        key,
        typeof value === "function"
          ? (...args: unknown[]) => {
              if (!detached) (value as (...a: unknown[]) => void)(...args);
            }
          : value,
      ]),
    ) as TurnStreamHandlers),
    onTaskState: (state) => {
      if (detached || !PAUSED.test(state)) return;
      paused = true;
      detached = true;
      onPaused();
      controller.abort(); // release the socket the server keeps open for the answer
    },
  };

  /** The turn is over: finalize it — and when its pause was taken over by another task,
   *  draw that task after it (fetched FIRST: once the bubble settles the slot cancels this
   *  reattach, so both land in one synchronous step). */
  async function settle(state: string, text: string, supersededBy: string | undefined) {
    const successors = supersededBy ? await successorTurns(supersededBy, () => cancelled) : [];
    if (cancelled) return;
    finalize(sessionId, assistantId, state, text);
    appendSuccessorTurns(sessionId, successors);
  }

  /** Paused on the operator: un-busy the session so the re-rendered form's buttons work,
   *  but DON'T finalize — stamping the message "done" would misrepresent a turn the
   *  server still owns. The bubble is marked paused instead, so it and its in-flight
   *  `ask_human` card render as waiting rather than spinning (#3946). */
  function settlePaused() {
    const cur = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
    if (cur) chatStore.updateMessages(sessionId, markTurnPaused(cur.messages, assistantId));
    chatStore.setSessionStatus(sessionId, "idle");
  }

  async function run() {
    chatStore.setSessionStatus(sessionId, "streaming");
    for (let attempt = 0; attempt < MAX_ATTEMPTS && !cancelled; attempt++) {
      try {
        const subscription = Promise.resolve(api.resumeTask(taskId, sessionId, streamHandlers));
        // Once paused, the subscription's own end (an abort error, or never on desktop)
        // is no longer anyone's business.
        subscription.catch(() => {});
        const outcome = await Promise.race([subscription.then(() => "closed" as const), pausedSignal]);
        if (cancelled) return;
        if (outcome === "paused") {
          settlePaused();
          return;
        }
        // Stream closed = the turn is over (terminal-by-state, A2A 1.0). Confirm
        // and finalize off the durable task.
        const { state, text, supersededBy } = await api
          .getTask(taskId)
          .catch(() => ({ state: "completed", text: "", supersededBy: undefined }));
        // Cancelled while GetTask was out: the cancel already handed the session back, and a
        // turn started since owns it now — finalize would set it idle mid-turn (Stop gone,
        // Send live), inviting a second concurrent turn into this slot.
        if (cancelled) return;
        if (PAUSED.test(state)) {
          // Paused with no paused frame on the stream (an older server, or a stream that
          // closed as the task parked): same settle.
          settlePaused();
          return;
        }
        await settle(state || "completed", text, supersededBy);
        return;
      } catch (err) {
        if (cancelled) return;
        if (paused) {
          settlePaused();
          return;
        }
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

  void run()
    .catch(() => {
      /* reattach is best-effort — never crash the surface */
    })
    // The run's own end: finalize or the paused path has usually settled the status already,
    // and a run that gave up (retries or polls spent) leaves it to the reconciler.
    .finally(letGo);

  return () => {
    cancelled = true;
    controller.abort();
    letGo();
  };
}

/** What a chat slot runs on mount and whenever its reattach key changes: resubscribe to the
 *  lead turn's still-streaming bubble, or, when there is nothing to reattach, reconcile.
 *
 *  The reconcile is what an opened session needs when its slot never mounted while its turn
 *  ran. Boot marks every session with a live turn "streaming", but only
 *  MAX_ACTIVE_SESSIONS slots mount, so a sixth one's turn can end (its preview settled by
 *  `chat.resumed`) with no slot and no reattach to hand the status back. Opening it then
 *  finds no streaming bubble and nothing to reattach. Before, it sat "streaming" for good;
 *  now the reconciler reads that nothing is live and returns it to idle. */
export function reattachOrReconcile(sessionId: string, hooks: ReattachHooks = {}): (() => void) | undefined {
  const snap = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
  // The same bubble `reattachKey` names: a participant's row after the preview is not it.
  const last = leadAssistantMessage(snap?.messages);
  // Not a server-fired turn this console is watching live: the bus already feeds that
  // preview, and a second producer wrote every chunk twice (see shouldReattach).
  if (!shouldReattach(last, sessionId)) {
    reconcileSessionStatus(sessionId);
    return undefined;
  }
  return reattachTurn(sessionId, last.id, last.taskId, hooks);
}
