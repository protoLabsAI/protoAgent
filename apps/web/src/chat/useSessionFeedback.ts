import { useEffect, useMemo, useRef, useState } from "react";
import { feedbackApi, type FeedbackRating, type FeedbackTurn } from "../lib/api/feedback";
import type { ChatMessage } from "../lib/types";

export function useSessionFeedback(sessionId: string, messages: ChatMessage[], incognito = false) {
  const [turns, setTurns] = useState<FeedbackTurn[]>([]);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const generation = useRef(0);
  const busy = useRef(false);
  const write = useRef<ReturnType<typeof feedbackApi.set> | null>(null);
  const candidates = messages.filter((m) => m.role === "assistant" && m.status !== "streaming" && !m.author && !m.addressedTo && !m.report && !m.scheduled);
  const identity = JSON.stringify(candidates.map(({ id, taskId }) => ({ id, taskId })));
  // Streaming text elsewhere must not invalidate every settled message's actions.
  const assistants = useMemo(() => JSON.parse(identity) as Pick<ChatMessage, "id" | "taskId">[], [identity]);
  // One session read, shared by all rows. Refresh only when another turn settles so
  // newly recorded telemetry becomes rateable without reloading the conversation.
  const settled = assistants.map((m) => `${m.id ?? ""}:${m.taskId ?? ""}`).join("\n");
  useEffect(() => {
    const version = ++generation.current;
    setTurns([]);
    setError("");
    busy.current = false;
    setPending(false);
    if (!incognito && settled) {
      // If a new turn settles during a save, read after that write so a stale
      // GET cannot overwrite the optimistic rating.
      void Promise.resolve(write.current).catch(() => undefined).then(async () => {
        if (version !== generation.current) return;
        const data = await feedbackApi.get(sessionId);
        if (version === generation.current) setTurns(data.turns ?? []);
      }).catch(() => {
        if (version === generation.current) setError("Could not load feedback.");
      });
    }
    return () => { generation.current++; };
  }, [sessionId, incognito, settled]);

  return useMemo(() => ({
    error,
    pending,
    available: turns.length > 0,
    resolve(message: ChatMessage) {
      const index = assistants.findIndex((candidate) => message.id && candidate.id === message.id);
      if (incognito || index < 0) return undefined;
      if (message.taskId) {
        const matches = turns.filter((t) => t.task_id === message.taskId);
        return matches.length === 1 ? matches[0] : undefined;
      }
      // Only use the backend's ordered fallback when the complete sequences align.
      // Missing telemetry must never shift a rating onto a different turn.
      const aligned = turns.length === assistants.length && assistants.every((candidate, i) =>
        !candidate.taskId || candidate.taskId === turns[i].task_id);
      return aligned ? turns[index] : undefined;
    },
    async save(turn: FeedbackTurn, value: FeedbackRating, note = "") {
      if (incognito || busy.current) return;
      const version = generation.current;
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
        if (version === generation.current) setTurns((rows) => update(rows, result.rating, result.note ?? ""));
      } catch {
        if (version === generation.current) {
          setTurns(previous);
          setError("Could not save feedback. Please try again.");
        }
      } finally {
        if (version === generation.current) {
          busy.current = false;
          setPending(false);
        }
      }
    },
  }), [error, pending, turns, incognito, assistants, sessionId]);
}
export type SessionFeedbackActions = ReturnType<typeof useSessionFeedback>;
