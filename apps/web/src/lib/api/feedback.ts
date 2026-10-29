import { request } from "./http";

export type FeedbackRating = 0 | 1 | null;
export type FeedbackTurn = {
  task_id: string | null;
  trace_id: string | null;
  created_at: string;
  rating: FeedbackRating;
  note: string | null;
};
export type SessionFeedback = { enabled: boolean; tracing_enabled: boolean; turns: FeedbackTurn[] };
export type FeedbackRequest = {
  session_id: string;
  trace_id: string;
  value: FeedbackRating;
  note?: string;
  incognito: boolean;
};
export const feedbackApi = {
  get(sessionId: string) {
    return request<SessionFeedback>(`/api/feedback?session_id=${encodeURIComponent(sessionId)}`);
  },
  set(body: FeedbackRequest) {
    return request<{ ok: boolean; rating: FeedbackRating; note?: string }>("/api/feedback", { method: "POST", body });
  },
};
