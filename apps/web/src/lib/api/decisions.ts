/**
 * "Decision needed" client (#4725) — the read + answer surface over the durable decisions
 * ledger (`operator_api/decision_routes.py`, `graph/decisions.py`). An agent parks a turn with
 * `request_decision`; the console lists the pending ones, shows the question + options, answers
 * one (which resumes the parked turn), or cancels one.
 *
 * A standalone slice, deliberately NOT folded into the composed `api` object yet: nothing is
 * mounted (the data + unmounted components land first, #4861 split), so the store and the
 * components import `decisionsApi` directly. Shapes mirror the directive client (`./chat`).
 */
import { notifyAuthRequired } from "../auth";
import { ApiError, isMemberScoped, parseErrorBody, request } from "./http";
import { apiUrl, applyAuth } from "./routing";

/** One option the agent offered, from `graph.decisions.normalize_options`. */
export type DecisionOption = {
  id: string;
  label: string;
  /** One line of explanation from the agent, if any. */
  description?: string;
  /** At most one option may be marked recommended; the card shows it primary + "suggested". */
  recommended?: boolean;
};

export type DecisionState = "pending" | "answered" | "expired" | "cancelled" | "withdrawn";

/** Wire row from `graph.decisions.Decision.to_dict()`; timestamps are ISO (offsetless = UTC).
 *  Every server field is carried, including the two it computes (`default_action_text`,
 *  `answer_label`) and the agent/session link (`agent`, `session_id`, `thread_id`). */
export type DecisionRow = {
  id: string;
  session_id: string;
  agent: string;
  question: string;
  options: DecisionOption[];
  created_at: string;
  state: DecisionState;
  answer: string | null;
  answered_at: string | null;
  /** Optional expiry + the stated default; no expiry waits indefinitely. */
  expires_at: string | null;
  default_option: string | null;
  thread_id: string;
  interrupt_id: string;
  context: string;
  default_action: string | null;
  withdraw_reason: string | null;
  answered_by: string | null;
  answered_surface: string | null;
  /** Server-computed: the agent's default action verb phrase, or "carry on without this step". */
  default_action_text: string;
  /** Server-computed: the chosen option's label (or the raw answer for free text). */
  answer_label: string | null;
  /** Single-record reads only: an answer is on its way to the agent (accepted by the server,
   *  not yet consumed by the parked turn). Absent on list rows. */
  delivering?: boolean;
};

export type DecisionList = { decisions: DecisionRow[]; pending: number };

/** The console is the only answer surface this client speaks for. */
export type AnswerSurface = "console";

/** Answering resolves to one of two shapes. A 409 ("answered somewhere else") is NOT a throw —
 *  it is the server telling us the decision closed under us, carrying the now-closed record so
 *  the card can show "Already answered…". Any other non-2xx still throws an {@link ApiError}.
 *  `delivering` (a 202) means the answer is on its way but the agent hasn't taken it yet — the
 *  session is busy; the decision is still pending and must NOT be answered again (#5099). */
export type AnswerResult =
  | { answered_elsewhere: false; decision: DecisionRow; resumed: boolean; delivering?: boolean }
  | { answered_elsewhere: true; decision: DecisionRow | null };

export const decisionsApi = {
  /** Pending decisions (fleet-wide), soonest expiry then undated oldest first — `{decisions,
   *  pending}`. `state` narrows the ledger; the panel lists `pending`. */
  listDecisions(state: DecisionState = "pending"): Promise<DecisionList> {
    const query = state ? `?state=${encodeURIComponent(state)}` : "";
    return request<DecisionList>(`/api/decisions${query}`);
  },

  /** One decision by id (404 → {@link ApiError}). */
  getDecision(id: string): Promise<DecisionRow> {
    return request<DecisionRow>(`/api/decisions/${encodeURIComponent(id)}`);
  },

  /** Answer a pending decision with an option, from the console surface, resuming the parked
   *  turn. A 409 resolves to `{answered_elsewhere: true, decision}` rather than throwing: the
   *  server sends the now-closed record as the error `detail`, which the card reads to say who
   *  answered it and when. A direct `fetch` (not `request`) so the 409 body survives — the
   *  shared error parser collapses a dict `detail` to a bare message. */
  async answerDecision(
    id: string,
    body: { option_id: string; surface?: AnswerSurface },
  ): Promise<AnswerResult> {
    const path = `/api/decisions/${encodeURIComponent(id)}/answer`;
    const response = await fetch(apiUrl(path), {
      method: "POST",
      headers: applyAuth(new Headers({ "Content-Type": "application/json" })),
      body: JSON.stringify({ option_id: body.option_id, surface: body.surface ?? "console" }),
    });
    if (response.status === 409) {
      // Closed under us (answered in chat, expired, withdrawn). FastAPI wraps the record as
      // `{detail: <decision>}`; hand it back as the answered-elsewhere result.
      const payload = (await response.json().catch(() => null)) as { detail?: unknown } | null;
      const detail = payload && typeof payload === "object" ? payload.detail : null;
      return { answered_elsewhere: true, decision: (detail as DecisionRow) ?? null };
    }
    if (!response.ok) {
      const raw = await response.text().catch(() => "");
      const { detail, code } = parseErrorBody(raw, `${response.status} ${response.statusText}`);
      if (response.status === 401 && !isMemberScoped(path)) notifyAuthRequired();
      throw new ApiError(response.status, detail || "request failed", code);
    }
    const data = (await response.json()) as { decision: DecisionRow; resumed: boolean; delivering?: boolean };
    return { answered_elsewhere: false, decision: data.decision, resumed: data.resumed, delivering: Boolean(data.delivering) };
  },

  /** Cancel a pending decision (the operator dismissed it), unblocking the parked turn. */
  cancelDecision(id: string, reason = ""): Promise<{ decision: DecisionRow }> {
    return request<{ decision: DecisionRow }>(`/api/decisions/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      body: { reason },
    });
  },
};
