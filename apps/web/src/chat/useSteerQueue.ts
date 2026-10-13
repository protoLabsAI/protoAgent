import { useEffect, useRef, useState } from "react";
import type React from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import type { ChatMessage, ConsumedSteer, HitlPayload, QueuedSteer } from "../lib/types";
import { chatStore, type SessionStatus } from "./chat-store";
import { messageId } from "./messageId";
import { loadSteers, saveSteers } from "./scratchState";
import { liveMessageId } from "./server-turn-store";
import { placeConsumedSteers, placeServerTurnSteers } from "./steerPlacement";
import {
  planInterjectionReconcile,
  serverTurnPhase,
  staleInterjections,
  type ServerTurnPhase,
} from "./serverInterjections";

// Steer-queue ref initializer — mirrors the lazy state init (a useRef can't take
// an initializer function, and re-reading storage per render would be wasteful).
function steerQueueRef_init(sessionId: string): QueuedSteer[] {
  return loadSteers(sessionId);
}

export type UseSteerQueueOptions = {
  sessionId: string;
  /** The session this slot renders (`null` before hydration). Guards every server call. */
  session: { id: string } | null;
  /** The render's transcript (`session?.messages || []`). A queued id appearing here settles it. */
  messages: ChatMessage[];
  status: SessionStatus;
  visible: boolean;
  /** The attended server turn's live control frame, if any. */
  serverTurnControl: { taskId: string } | null;
  serverTurnLabel: string | null;
  draft: string;
  setDraft: React.Dispatch<React.SetStateAction<string>>;
  onError: (message: string) => void;
  /** The slot's turn runner: a reconcile re-sends never-seen messages as a fresh turn. */
  runTurn: (content: string) => Promise<void>;
  /** useHitl's ref mirror. A parked HITL turn keeps its unread steers queued. */
  hitlRef: React.MutableRefObject<HitlPayload | null>;
  /** The slot's live-stream controller. The idle reconcile skips while this slot owns a stream. */
  abortRef: React.MutableRefObject<AbortController | null>;
  /** Record a queued message in the composer's input history and reset history nav. */
  recordSubmitted: (text: string) => void;
};

// The mid-turn steer queue and its reconcile (#3862, extracted from ChatSessionSlot): the
// queued state (plus a ref mirror for async closures), the two ways a message joins it
// (a steer into this browser's own stream, an interjection into an attended server turn),
// the ✕/dequeue path, and every path that settles, re-targets, re-sends or hands back a
// queued message once its turn is over. Every function is a fresh per-render closure over
// the options, exactly as it was inline in the slot, so a timer or an await that started
// on render N still calls render N's `runTurn`, `session` and `onError`. The effects keep
// their dependency arrays, and the slot calls this hook where the first of them used to be,
// so they run in the same order relative to the slot's own effects.
export function useSteerQueue({
  sessionId,
  session,
  messages,
  status,
  visible,
  serverTurnControl,
  serverTurnLabel,
  draft,
  setDraft,
  onError,
  runTurn,
  hitlRef,
  abortRef,
  recordSubmitted,
}: UseSteerQueueOptions) {
  // Mid-turn steering: user messages queued WHILE a turn runs (optimistic), reconciled
  // at turn-end. ONE queue for both kinds — steers into this browser's own stream, and
  // interjections into an attended server turn (tagged `serverTaskId`) — because the
  // server drains both from one per-session steering queue: the same ✕ (DELETE …/steer)
  // takes either back, the same consumed marker settles either, and the transcript is
  // the single record of which ones have landed. The ref mirrors the state so the
  // post-stream reconcile (a stale render closure) reads the live queue.
  const [steerQueue, setSteerQueueState] = useState<QueuedSteer[]>(() => loadSteers(sessionId));
  const steerQueueRef = useRef<QueuedSteer[]>(steerQueueRef_init(sessionId));
  const setSteerQueue = (next: QueuedSteer[]) => {
    steerQueueRef.current = next;
    setSteerQueueState(next);
    saveSteers(sessionId, next); // scratch state survives a swap (S3)
  };
  // Interjections whose POST hasn't answered yet: the server hasn't said whether it even
  // queued them, so the server-turn reconcile must not read "absent from the queue" as
  // "consumed" for these.
  const interjectInFlightRef = useRef<Set<string>>(new Set());

  // The transcript is the one record of which queued messages have landed: whatever put a
  // queued id there — this stream's consumed marker, the bus's marker for a server turn
  // (ServerTurnWatch, which knows nothing of this queue), a turn-end reconcile — the
  // pending bubble retires here. Before this, a server-turn interjection had no settle
  // path at all and sat "queued" under an answer that had already used it.
  useEffect(() => {
    const queued = steerQueueRef.current;
    if (!queued.length) return;
    const settled = new Set(messages.map((message) => message.id));
    if (queued.some((item) => settled.has(item.id))) {
      setSteerQueue(queued.filter((item) => !settled.has(item.id)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setSteerQueue is a per-render wrapper over stable state
  }, [messages]);

  // Reconcile interjections whose server turn is no longer the live one — the turn ended,
  // was stopped, or this slot mounted with some queued from before a reload. Keyed on the
  // live control's TASK id, not the control object (every progress frame re-sends it), plus
  // the turn label (a `turn.finished` this slot saw without ever seeing a control frame).
  // The reconcile asks the server before it acts, so an extra run is a no-op, never a guess.
  const liveServerTaskId = serverTurnControl?.taskId ?? "";
  const sessionReady = Boolean(session);
  useEffect(() => {
    if (sessionReady) {
      // Re-check NOW (the turn's state just changed) but keep the grace: only a clean sweep
      // ends an episode. A trigger that refunded it would let an unresolvable message stay
      // "queued" for as long as anything kept re-triggering — which a reload, a second turn
      // or a focus change all do.
      cancelInterjectRecheck();
      void reconcileServerInterjections();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the reconcile reads live store state
  }, [liveServerTaskId, serverTurnLabel, sessionReady]);

  // A turn this slot did NOT run can also end: a reattached turn (reload / agent switch)
  // settles through reattach.ts, which never reaches runTurn's turn-end reconcile — so a
  // steer queued before the reload sat "queued" until the operator's next turn. Reconcile
  // when the session goes idle with no locally-owned stream; it is a no-op with nothing
  // queued, and a parked HITL turn keeps its steers (reconcileSteer's own rule).
  useEffect(() => {
    if (status !== "idle" || abortRef.current) return;
    if (steerQueueRef.current.some((queued) => !queued.serverTaskId)) void reconcileSteer();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reconcileSteer reads live store state
  }, [status, sessionReady]);

  // Steer a RUNNING turn: queue the typed message (folds in at the agent's next
  // model call via SteeringMiddleware) without stopping the stream. Shows an
  // optimistic "queued" bubble; turn-end reconcile settles or re-sends it.
  async function queueSteer() {
    const text = draft.trim();
    if (!session || !text) return;
    recordSubmitted(text); // steered messages join the same recall ring
    const id = messageId();
    setDraft("");
    setSteerQueue([...steerQueueRef.current, { id, text }]);
    try {
      await api.steerChat(session.id, id, text);
    } catch (e) {
      setSteerQueue(steerQueueRef.current.filter((x) => x.id !== id));
      onError(`Couldn't queue message: ${errMsg(e)}`);
    }
  }

  // Interject into an ATTENDED server-initiated turn without starting a competing
  // browser-owned turn. The durable task id comes from bd-v92b control frames, not
  // from local stream ownership, so this also works after remount when the backend
  // republishes the live control contract. The server drops the text into the ordinary
  // steering queue, so it joins the same queue as a steer — tagged with the turn it was
  // sent to, which is how the reconcile knows when that turn is over.
  async function queueServerInterjection() {
    const text = draft.trim();
    const control = chatStore.getSnapshot().serverTurnControls[sessionId];
    if (!session || !text || !control) return;
    recordSubmitted(text);
    const id = messageId();
    setDraft("");
    // Queued UNCONFIRMED until the server answers: persisted that way too, so a reload
    // mid-flight (or a request that never answers) comes back knowing the console never
    // learned whether the server has it — see QueuedSteer.unconfirmed.
    setSteerQueue([...steerQueueRef.current, { id, text, serverTaskId: control.taskId, unconfirmed: true }]);
    interjectInFlightRef.current.add(id);
    try {
      const res = await api.serverTurnInterject(session.id, control.taskId, id, text);
      if (res.ok || res.reason === "duplicate") {
        // Acknowledged: the server has it, so its absence from the queue later can be read
        // as "drained" rather than "never arrived".
        setSteerQueue(steerQueueRef.current.map((x) => (x.id === id ? { id, text, serverTaskId: control.taskId } : x)));
      } else {
        // Refused, and answered 200: the turn ended (or stopped taking interjections)
        // between its last control frame and this POST. Nothing was queued, so nothing will
        // ever settle this bubble — it used to sit "queued" regardless. Take it back, give
        // the operator the words (never silently — this is the most reachable window there
        // is: interjecting while the final answer streams), and drop the control the server
        // just called stale so the next Enter sends a normal message instead of being
        // refused again.
        handBackQueued(
          [{ id, text }],
          res.reason === "not_live"
            ? "That server task had already finished, so your message wasn't sent — its text is in the composer."
            : "That server task isn't taking messages right now, so yours wasn't sent — its text is in the composer.",
        );
        chatStore.clearServerTurnControl(sessionId, control.taskId);
      }
    } catch (e) {
      // No answer at all. The server may well have queued it, so the bubble STAYS (dropping
      // it while the agent goes on to read the message is what invites a duplicate send);
      // it keeps `unconfirmed`, and the reconcile resolves it from the steering queue and
      // the durable marker — handing the words back only once nothing can account for them.
      onError(`Couldn't confirm your message reached the turn: ${errMsg(e)}`);
    } finally {
      interjectInFlightRef.current.delete(id);
      // The turn may have ended while this was in flight; the reconcile skipped it then.
      // Cancel, don't reset: an OLDER unresolved interjection must keep its grace.
      cancelInterjectRecheck();
      void reconcileServerInterjections();
    }
  }

  /** Give the operator back words that were never delivered: out of the queue, into the
   *  composer (APPENDED — an in-hand draft is never destroyed to undo our own optimism,
   *  #3413), and said out loud. The one place that hands text back, so the toast can't
   *  promise something a caller forgot to do. */
  function handBackQueued(items: { id: string; text: string }[], message: string) {
    if (!items.length) return;
    const ids = new Set(items.map((item) => item.id));
    setSteerQueue(steerQueueRef.current.filter((q) => !ids.has(q.id)));
    const text = items.map((item) => item.text).join("\n\n");
    setDraft((current) => (current.trim() ? `${current.replace(/\s+$/, "")}\n\n${text}` : text));
    onError(message);
  }

  // Settle interjections a server turn consumed but whose boundary marker this console
  // never saw — conservatively above that turn's reply (steerPlacement.ts). Grouped per
  // turn, because each anchors to its own turn's bubbles.
  function settleServerInterjections(items: QueuedSteer[]) {
    if (!session || !items.length) return;
    // Read the transcript BEFORE retiring anything from the queue: a bail-out between the
    // two would drop the bubble without ever placing the message.
    let next = chatStore.getSnapshot().sessions.find((s) => s.id === session.id)?.messages;
    if (!next) return;
    const ids = new Set(items.map((item) => item.id));
    setSteerQueue(steerQueueRef.current.filter((q) => !ids.has(q.id)));
    const byTask = new Map<string, QueuedSteer[]>();
    for (const item of items) {
      const key = item.serverTaskId ?? "";
      byTask.set(key, [...(byTask.get(key) ?? []), item]);
    }
    for (const [taskId, group] of byTask) {
      next = placeServerTurnSteers(next, group, {
        liveId: liveMessageId(taskId, session.id),
        exact: false,
        frozenId: messageId(),
        createdAt: Date.now(),
      });
    }
    chatStore.updateMessages(session.id, next);
  }

  // The server-turn counterpart of reconcileSteer: interjections sent to a server turn
  // that is no longer the live one are settled, left, re-targeted, re-sent or handed back,
  // decided from the server's own steering queue and the turn's DURABLE task
  // (serverInterjections.ts). Serialised — the triggers can fire together — and re-run if
  // one arrives mid-flight.
  const interjectReconcileRef = useRef<{ running: boolean; again: boolean }>({ running: false, again: false });
  // Its own-stream sibling's guard (reconcileSteer): one reconcile per turn-end, whichever
  // caller gets there first.
  const steerReconcileRef = useRef(false);
  // Nothing here is a one-shot: a transient failure, or a turn whose task hasn't settled
  // yet, re-checks on this ladder. A one-shot reconcile strands the very bubble it exists
  // to retire — and the message stays in the server's queue for some later turn, unseen.
  // The ladder only runs while an unresolved interjection is queued (so it is bounded by
  // something the operator can see), and any explicit trigger restarts it from the top.
  // `steps` paces the ladder; `asks` counts SUCCESSFUL server reads and is the grace an
  // unaccounted-for item gets before the console stops calling it queued. They are separate
  // because the things that may restart the timer (coming back to the tab, a fresh control
  // frame) must not also restart the grace — churn would make an unresolvable message
  // unresolvable forever — and because reads that never reached the server must not spend
  // it. Both zero only on a clean sweep, which is what ends the episode.
  const interjectRetryRef = useRef<{ timer: number | null; steps: number; asks: number }>({
    timer: null,
    steps: 0,
    asks: 0,
  });
  const INTERJECT_RECHECK_MS = [1000, 2000, 4000, 8000, 15000, 30000];

  function scheduleInterjectRecheck() {
    const retry = interjectRetryRef.current;
    if (retry.timer !== null) return;
    const delay = INTERJECT_RECHECK_MS[Math.min(retry.steps, INTERJECT_RECHECK_MS.length - 1)];
    retry.steps += 1;
    retry.timer = window.setTimeout(() => {
      retry.timer = null;
      void reconcileServerInterjections();
    }, delay);
  }

  /** Cancel a pending re-check without touching the grace — for a trigger that is about to
   *  reconcile immediately anyway. */
  function cancelInterjectRecheck() {
    const retry = interjectRetryRef.current;
    if (retry.timer !== null) window.clearTimeout(retry.timer);
    retry.timer = null;
  }

  /** Nothing left unresolved: end the episode, ladder and grace both. */
  function resetInterjectRecheck() {
    cancelInterjectRecheck();
    interjectRetryRef.current.steps = 0;
    interjectRetryRef.current.asks = 0;
  }

  useEffect(() => cancelInterjectRecheck, []); // never leave a timer behind on unmount

  // A tab that was asleep (or offline) may have missed the live-only frames entirely, so
  // coming back is itself a reason to re-check now rather than wait out the ladder — but
  // only the TIMER is dropped: alt-tabbing must not keep an unresolvable message alive by
  // refunding its grace.
  useEffect(() => {
    if (!visible) return;
    const recheck = () => {
      if (document.visibilityState === "hidden") return;
      cancelInterjectRecheck();
      void reconcileServerInterjections();
    };
    window.addEventListener("focus", recheck);
    document.addEventListener("visibilitychange", recheck);
    return () => {
      window.removeEventListener("focus", recheck);
      document.removeEventListener("visibilitychange", recheck);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the reconcile reads live store state
  }, [visible, sessionId]);

  async function reconcileServerInterjections() {
    const guard = interjectReconcileRef.current;
    if (guard.running) {
      guard.again = true;
      return;
    }
    guard.running = true;
    try {
      do {
        guard.again = false;
        await reconcileServerInterjectionsOnce();
      } while (guard.again);
    } finally {
      guard.running = false;
    }
  }

  async function reconcileServerInterjectionsOnce() {
    if (!session) return;
    const liveTaskId = () => chatStore.getSnapshot().serverTurnControls[sessionId]?.taskId ?? "";
    const stale = staleInterjections(steerQueueRef.current, liveTaskId(), interjectInFlightRef.current);
    if (!stale.length) {
      resetInterjectRecheck();
      return;
    }
    let pendingIds: Set<string>;
    let drainedIds: Set<string>;
    try {
      const queue = await api.pendingSteer(session.id);
      pendingIds = new Set(queue.pending.map((item) => item.id));
      drainedIds = new Set(queue.drained ?? []);
      // Only a read that REACHED the server spends the grace: a blip must not consume the
      // very budget that exists to survive it.
      interjectRetryRef.current.asks += 1;
    } catch {
      scheduleInterjectRecheck(); // transient: ask again, never guess from a failed read
      return;
    }
    const phases = new Map<string, ServerTurnPhase>();
    const consumedIds = new Map<string, Set<string>>();
    for (const taskId of new Set(stale.map((q) => q.serverTaskId ?? ""))) {
      const read = await api
        .taskSteerState(taskId)
        .catch(() => ({ state: "", consumed: [] as string[] }));
      phases.set(taskId, serverTurnPhase(read.state));
      consumedIds.set(taskId, new Set(read.consumed));
    }
    // The awaits above yield: decide against the queue and turn state as they are NOW —
    // an item may have been cancelled or settled by a marker meanwhile.
    const stillQueued = new Set(steerQueueRef.current.map((q) => q.id));
    const plan = planInterjectionReconcile(
      stale.filter((q) => stillQueued.has(q.id)),
      {
        pendingIds,
        drainedIds,
        phases,
        consumedIds,
        ownStreamLive: chatStore.getSnapshot().sessionStatusMap[sessionId] === "streaming",
        liveServerTaskId: liveTaskId(),
        hitlPending: Boolean(hitlRef.current),
        asks: interjectRetryRef.current.asks,
      },
    );
    if (plan.settle.length) settleServerInterjections(plan.settle);
    if (plan.retarget.length) {
      const next = new Map(plan.retarget.map((item) => [item.id, item]));
      setSteerQueue(steerQueueRef.current.map((q) => next.get(q.id) ?? q));
    }
    if (plan.reclaim.length) await reclaimInterjections(plan.reclaim);
    if (plan.resend.length) await resendInterjections(plan.resend);
    // Re-check while ANY stale interjection is still queued — read from the live queue, not
    // from the plan: a dequeue or a hand-off can fail inside the steps above and leave an
    // item behind, and cancelling the ladder then strands exactly what it exists to retire.
    if (staleInterjections(steerQueueRef.current, liveTaskId(), interjectInFlightRef.current).length) {
      scheduleInterjectRecheck();
    } else {
      resetInterjectRecheck();
    }
  }

  /** A submission the server never acknowledged and can't account for. Take it back FIRST:
   *  that is what makes handing the words over safe — a copy the server still held could
   *  otherwise be read after the operator was told it wasn't sent.
   *
   *  The dequeue's ANSWER then decides, because it is the only thing that knows what the
   *  read a moment ago could not. `removed` means we hold the only copy: the words are the
   *  operator's again. `removed: false` means we took nothing back — which is exactly the
   *  window this dequeue exists to close, inverted: a turn can fold the message in between
   *  the read and the dequeue. So ask once more before speaking: if the server's drain log
   *  now names it, the agent has it and the bubble settles; only a server that can keep a
   *  drain log AND reports neither a queued nor a drained copy has actually shown the
   *  message never landed. Anything less definite settles rather than re-offering words the
   *  agent may have used. */
  async function reclaimInterjections(items: QueuedSteer[]) {
    if (!session) return;
    const back: QueuedSteer[] = [];
    const read: QueuedSteer[] = [];
    for (const item of items) {
      let removed: boolean;
      try {
        ({ removed } = await api.cancelSteer(session.id, item.id));
      } catch {
        scheduleInterjectRecheck(); // couldn't take it back — leave it queued and ask again
        continue;
      }
      if (removed) {
        back.push(item);
        continue;
      }
      let queue: { pending: { id: string }[]; drained?: string[] };
      try {
        queue = await api.pendingSteer(session.id);
      } catch {
        scheduleInterjectRecheck(); // the answer exists, we just couldn't read it — retry
        continue;
      }
      if (queue.pending.some((row) => row.id === item.id)) {
        scheduleInterjectRecheck(); // it landed after all: the ordinary rules apply next pass
      } else if (Array.isArray(queue.drained) && !queue.drained.includes(item.id)) {
        back.push(item); // this server tracks folds and has none of it: it never landed
      } else {
        read.push(item); // the agent has it (or nothing can say it doesn't)
      }
    }
    if (read.length) settleServerInterjections(read);
    if (back.length) {
      handBackQueued(
        back,
        back.length > 1
          ? "Those messages never reached the agent, and the turn they were waiting for is gone — their text is in the composer."
          : "That message never reached the agent, and the turn it was waiting for is gone — its text is in the composer.",
      );
    }
  }

  // An interjection its server turn never reached, with nothing left to drain it: take it
  // OUT of the server queue first — left there, the next turn would fold it in on top of
  // this re-send and the agent would read it twice — then send it as the operator's next
  // message. A dequeue answering `removed: false` lost a race to some turn that consumed it
  // after all, so that one settles instead. A dequeue that fails stays queued for the next
  // reconcile rather than being sent while the server may still hold it.
  async function resendInterjections(items: QueuedSteer[]) {
    if (!session) return;
    const send: QueuedSteer[] = [];
    const consumed: QueuedSteer[] = [];
    for (const item of items) {
      try {
        const { removed } = await api.cancelSteer(session.id, item.id);
        (removed ? send : consumed).push(item);
      } catch {
        scheduleInterjectRecheck(); // still (maybe) queued server-side — ask again later
      }
    }
    if (consumed.length) settleServerInterjections(consumed);
    if (!send.length) return;
    // Those dequeues yielded. If a turn started in this slot meanwhile — the operator sent
    // something, or another server turn came up — a fresh `runTurn` here would be a SECOND
    // concurrent stream in one slot: two live bubbles, one abort controller, and a Stop that
    // reaches only one of them. Hand the text to the turn that is running instead; it drains
    // the same queue, and its consumed marker settles these bubbles.
    const snap = chatStore.getSnapshot();
    const live = snap.serverTurnControls[sessionId];
    if (snap.sessionStatusMap[sessionId] === "streaming" || live || hitlRef.current) {
      await requeueIntoLiveTurn(send, live?.taskId);
      return;
    }
    const ids = new Set(send.map((item) => item.id));
    setSteerQueue(steerQueueRef.current.filter((q) => !ids.has(q.id)));
    void runTurn(send.map((item) => item.text).join("\n\n"));
  }

  /** Put dequeued interjections back on the steering queue for the turn that is running
   *  NOW, keeping their bubbles queued so that turn's marker settles them. Their text is
   *  already out of the server's queue, so a failure here would lose it — hand it back. */
  async function requeueIntoLiveTurn(items: QueuedSteer[], serverTaskId: string | undefined) {
    if (!session) return;
    const lost = "Your message couldn't be handed to the running turn — its text is in the composer.";
    for (const item of items) {
      try {
        // An attended server turn takes it through its control task (the same guarded path
        // Enter uses); this browser's own stream takes it as an ordinary steer.
        if (serverTaskId) {
          const res = await api.serverTurnInterject(session.id, serverTaskId, item.id, item.text);
          if (!res.ok && res.reason !== "duplicate") {
            handBackQueued([item], lost);
            continue;
          }
        } else {
          await api.steerChat(session.id, item.id, item.text);
        }
        setSteerQueue(
          steerQueueRef.current.map((q) =>
            q.id === item.id ? { id: q.id, text: q.text, ...(serverTaskId ? { serverTaskId } : {}) } : q,
          ),
        );
      } catch {
        handBackQueued([item], lost);
      }
    }
  }

  // Take a still-queued steer back out of the running turn — the shared body behind the ✕
  // (cancel it) and ↑ (pull it into the composer to edit). Drops the bubble optimistically
  // so either interaction feels instant, then DELETEs it server-side. If the agent had
  // already drained it (`removed: false`), it's too late — it shaped the reply, so the
  // bubble is restored instead of lying it never ran. The outcome tells the ↑ caller
  // whether the message is really out of the turn or still on its way to the agent.
  async function dequeueSteer(id: string): Promise<"removed" | "consumed" | "failed"> {
    if (!session) return "failed";
    const item = steerQueueRef.current.find((q) => q.id === id);
    if (!item) return "failed";
    setSteerQueue(steerQueueRef.current.filter((q) => q.id !== id));
    try {
      const { removed } = await api.cancelSteer(session.id, id);
      if (!removed) {
        // Already consumed, but the authoritative stream marker may still be in
        // flight. Restore the queued bubble and let that marker place it exactly;
        // turn-end reconcile is the missed-marker fallback. Settling here raced the
        // marker and permanently pinned the steer to the legacy top-of-turn slot.
        const snap = chatStore.getSnapshot().sessions.find((row) => row.id === session.id);
        const alreadySettled = snap?.messages.some((message) => message.id === id);
        if (alreadySettled) return "consumed";
        // A server-turn interjection whose turn is no longer live has no marker left to
        // wait for — restoring it would park it "queued" forever. Settle it now, above the
        // reply it shaped. (While its turn is live, the bus marker is on its way: restore.)
        const liveTask = chatStore.getSnapshot().serverTurnControls[sessionId]?.taskId;
        if (item.serverTaskId && item.serverTaskId !== liveTask) {
          settleServerInterjections([item]);
          return "consumed";
        }
        if (!steerQueueRef.current.some((queued) => queued.id === id)) {
          setSteerQueue([...steerQueueRef.current, item]);
        }
        return "consumed";
      }
      return "removed";
    } catch (e) {
      // Couldn't reach the backend — restore the bubble rather than drop a steer
      // that may still be queued (avoid concurrent-add clobber by re-checking).
      if (!steerQueueRef.current.some((q) => q.id === id)) {
        setSteerQueue([...steerQueueRef.current, item]);
      }
      onError(`Couldn't cancel message: ${errMsg(e)}`);
      return "failed";
    }
  }

  // The ✕ on a pending bubble: drop the queued steer outright.
  async function cancelSteer(id: string) {
    await dequeueSteer(id);
  }

  // Settle steered messages the agent has folded in: an explicit stream marker
  // inserts them at the exact live boundary; polling/turn-end callers omit the id
  // and retain the conservative before-assistant fallback. All paths dedupe by id.
  function settleConsumed(consumed: ConsumedSteer[], inlineAssistantId?: string) {
    if (!session || !consumed.length) return;
    const consumedIds = new Set(consumed.map((c) => c.id));
    setSteerQueue(steerQueueRef.current.filter((q) => !consumedIds.has(q.id)));
    const snap = chatStore.getSnapshot().sessions.find((s) => s.id === session.id);
    if (!snap) return;
    chatStore.updateMessages(
      session.id,
      placeConsumedSteers(snap.messages, consumed, {
        inlineAssistantId,
        frozenId: messageId(),
        createdAt: Date.now(),
      }),
    );
  }

  // After a turn ends, reconcile any still-queued steers: those the agent folded
  // in settle into the thread; those still queued arrived after the last model
  // call (never seen) → re-send as a fresh turn so they aren't lost. Steers into THIS
  // stream only — an interjection sent to a server turn answers to that turn's
  // lifecycle (reconcileServerInterjections), and is left exactly as it is here.
  async function reconcileSteer() {
    const queued = steerQueueRef.current.filter((q) => !q.serverTaskId);
    if (!session || !queued.length) return;
    // Two callers can land on the same turn-end: the send loop when its own stream closes,
    // and the idle effect for a turn this slot didn't run. Both re-sending the same
    // un-consumed steer would deliver it twice, so only one runs.
    if (steerReconcileRef.current) return;
    steerReconcileRef.current = true;
    try {
      await reconcileSteerOnce(queued);
    } finally {
      steerReconcileRef.current = false;
    }
  }

  async function reconcileSteerOnce(queued: QueuedSteer[]) {
    if (!session) return;
    let remaining: { id: string; text: string }[];
    try {
      remaining = (await api.pendingSteer(session.id)).pending;
    } catch {
      return; // can't tell consumed from not — leave the queue rather than guess
    }
    const remainingIds = new Set(remaining.map((r) => r.id));
    const consumed = queued.filter((q) => !remainingIds.has(q.id));
    const unconsumed = queued.filter((q) => remainingIds.has(q.id));
    if (consumed.length) settleConsumed(consumed);
    // The turn parked on a HITL form (#1560): steers the agent hasn't folded yet stay
    // QUEUED — the server keeps holding them and folds them in right after the form
    // response. Re-sending them as a fresh turn here would deliver them BEFORE the
    // form answer (and abandon the pending interrupt).
    const reconciled = new Set(queued.map((q) => q.id));
    if (unconsumed.length && hitlRef.current) {
      const keep = new Set(unconsumed.map((q) => q.id));
      setSteerQueue(steerQueueRef.current.filter((q) => !reconciled.has(q.id) || keep.has(q.id)));
      return;
    }
    setSteerQueue(steerQueueRef.current.filter((q) => !reconciled.has(q.id)));
    if (unconsumed.length) {
      void runTurn(unconsumed.map((u) => u.text).join("\n\n"));
    }
  }

  /** Settle Stop's account with the server for every message that was still queued.
   *
   *  Stop clears those bubbles instantly, but the SERVER's copy has to be dealt with or the
   *  next turn in this chat folds in a message the operator just watched disappear. The
   *  dequeue answers which it was: `removed` means the agent never saw it, so the words go
   *  back to the operator rather than nowhere; `removed: false` means it had already been
   *  read, so it settles into the transcript instead of vanishing. Best-effort by nature —
   *  an unreachable server hands the words back too, which loses nothing. */
  async function dropQueuedOnStop(items: QueuedSteer[]) {
    if (!items.length) return;
    const back: QueuedSteer[] = [];
    const consumed: QueuedSteer[] = [];
    for (const item of items) {
      try {
        const { removed } = await api.cancelSteer(sessionId, item.id);
        (removed ? back : consumed).push(item);
      } catch {
        back.push(item);
      }
    }
    const serverTurn = consumed.filter((item) => item.serverTaskId);
    const ownStream = consumed.filter((item) => !item.serverTaskId);
    if (serverTurn.length) settleServerInterjections(serverTurn);
    if (ownStream.length) settleConsumed(ownStream);
    if (back.length) {
      handBackQueued(
        back,
        back.length > 1
          ? "Those queued messages were never sent — their text is in the composer."
          : "That queued message was never sent — its text is in the composer.",
      );
    }
  }

  /** Stop's share of the queue: clear the optimistic bubbles now, end the re-check episode,
   *  and settle the server's copy of each (dropQueuedOnStop). */
  function clearQueueOnStop() {
    const dropped = steerQueueRef.current;
    setSteerQueue([]);
    resetInterjectRecheck();
    void dropQueuedOnStop(dropped);
  }

  return {
    steerQueue,
    steerQueueRef,
    queueSteer,
    queueServerInterjection,
    dequeueSteer,
    cancelSteer,
    settleConsumed,
    reconcileSteer,
    clearQueueOnStop,
  };
}
