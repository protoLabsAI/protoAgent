import { useEffect, useMemo, useRef, useState } from "react";
import { feedbackApi, type FeedbackRating, type FeedbackTurn } from "../lib/api/feedback";
import type { ChatMessage } from "../lib/types";

export function useSessionFeedback(sessionId: string, messages: ChatMessage[], incognito = false) {
  const [turns, setTurns] = useState<FeedbackTurn[]>([]);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  // Bumps only when the session (or its incognito flag) changes, so a late GET/save from the
  // previous session cannot land on this one. A new turn merely settling does NOT bump it —
  // that would invalidate an in-flight save on an unrelated message.
  const session = useRef(0);
  const busy = useRef(false);
  const write = useRef<ReturnType<typeof feedbackApi.set> | null>(null);
  const candidates = messages.filter((m) => m.role === "assistant" && m.status !== "streaming" && !m.author && !m.addressedTo && !m.report && !m.scheduled);
  const identity = JSON.stringify(candidates.map(({ id, taskId }) => ({ id, taskId })));
  // Streaming text elsewhere must not invalidate every settled message's actions.
  const assistants = useMemo(() => JSON.parse(identity) as Pick<ChatMessage, "id" | "taskId">[], [identity]);
  // One session read, shared by all rows. Refresh only when another turn settles so
  // newly recorded telemetry becomes rateable without reloading the conversation.
  const settled = assistants.map((m) => `${m.id ?? ""}:${m.taskId ?? ""}`).join("\n");

  // Switching session (or toggling incognito) is the ONLY clean slate: drop the old session's
  // ratings and any in-flight work. Deliberately NOT keyed on `settled` — clearing the rows on
  // every newly settled turn blanks the thumbs mid-conversation and unmounts a note the user is
  // typing, and a failed refresh would then leave the session with no ratings at all.
  useEffect(() => {
    session.current++;
    busy.current = false;
    write.current = null;
    setTurns([]);
    setError("");
    setPending(false);
  }, [sessionId, incognito]);

  useEffect(() => {
    if (incognito || !settled) return;
    const token = session.current;
    let cancelled = false;
    // If a new turn settles during a save, read after that write so a stale GET cannot
    // overwrite the optimistic rating. Keep the current rows visible while refreshing — a
    // failed refresh surfaces an error but leaves the existing ratings in place.
    void Promise.resolve(write.current).catch(() => undefined).then(async () => {
      if (cancelled || token !== session.current) return;
      const data = await feedbackApi.get(sessionId);
      if (!cancelled && token === session.current) {
        setTurns(data.turns ?? []);
        setError("");
      }
    }).catch(() => {
      if (!cancelled && token === session.current) setError("Could not load feedback.");
    });
    return () => { cancelled = true; };
  }, [sessionId, incognito, settled]);

  return useMemo(() => {
    // Collapse turn legs into logical turns. A HITL park/resume records two legs that SHARE one
    // task_id (telemetry_store: "one row per turn LEG"); the final leg carries the completed
    // turn's trace and output, so it represents the turn. Rows always carry a task_id (the store
    // drops any without one), so grouping by task_id — last leg wins in the oldest-first order the
    // GET returns — yields exactly one rateable entry per rendered assistant turn.
    const byTask = new Map<string, FeedbackTurn>();
    const order: string[] = [];
    for (const turn of turns) {
      const key = turn.task_id ?? "";
      if (!byTask.has(key)) order.push(key);
      byTask.set(key, turn);
    }
    const logical = order.map((key) => byTask.get(key)!);
    return {
      error,
      pending,
      available: logical.length > 0,
      resolve(message: ChatMessage): FeedbackTurn | undefined {
        const index = assistants.findIndex((candidate) => message.id && candidate.id === message.id);
        if (incognito || index < 0) return undefined;
        // A message with a task id maps straight to its turn's final leg — the one HITL legs
        // collapsed onto above. `undefined` when nothing recorded (an ACP/tool-only turn).
        if (message.taskId) return byTask.get(message.taskId);
        // Fallback for a message with no task id: join by turn ORDER. Only when the collapsed
        // sequences align exactly, so missing telemetry can never shift a rating onto a
        // different turn.
        const aligned = logical.length === assistants.length && assistants.every((candidate, i) =>
          !candidate.taskId || candidate.taskId === logical[i].task_id);
        return aligned ? logical[index] : undefined;
      },
      async save(turn: FeedbackTurn, value: FeedbackRating, note = "") {
        if (incognito || busy.current) return;
        const token = session.current;
        const traceId = turn.trace_id || "";
        const previous = turns;
        const update = (rows: FeedbackTurn[], rating: FeedbackRating, text: string) => rows.map((row) =>
          (row.trace_id || "") === traceId ? { ...row, rating, note: text } : row);
        busy.current = true;
        setPending(true);
        setError("");
        setTurns(update(turns, value, note));
        try {
          write.current = feedbackApi.set({ session_id: sessionId, trace_id: traceId, value, note, incognito });
          const result = await write.current;
          if (token === session.current) setTurns((rows) => update(rows, result.rating, result.note ?? ""));
        } catch {
          if (token === session.current) {
            setTurns(previous);
            setError("Could not save feedback. Please try again.");
          }
        } finally {
          if (token === session.current) {
            busy.current = false;
            setPending(false);
          }
        }
      },
    };
  }, [error, pending, turns, incognito, assistants, sessionId]);
}
export type SessionFeedbackActions = ReturnType<typeof useSessionFeedback>;
