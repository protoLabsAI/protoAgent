import { useRef, useState } from "react";

import { api } from "../lib/api";
import type { HitlPayload, SystemNoteTone } from "../lib/types";

/** The slot's `runTurn` signature, narrowed to the options a HITL answer sends. */
export type HitlRunTurn = (
  content: string,
  opts?: { hidden?: boolean; resumeMessageId?: string; hitlResume?: boolean },
) => Promise<void>;

export type UseHitlOptions = {
  /** The session this slot renders (`null` before hydration). Only its id is read. */
  sessionId: string | null;
  /** The slot's turn runner. The handlers call the one passed on the same render. */
  runTurn: HitlRunTurn;
  /** The slot's local system-note seam. A plugin form's reply and errors go here. */
  noteToThread: (text: string, opts?: { tone?: SystemNoteTone }) => void;
  /** The render's most recent operator-initiated assistant id. The slot computes it with
   *  a memo that sits BELOW this hook call, so it is passed as a thunk. The handlers read
   *  it only from events, and by then that render's value is set. */
  lastAssistantId: () => string | undefined;
};

// The pending HITL interrupt (#3862, extracted from ChatSessionSlot): an input-required
// form, question or approval the agent parked its turn on, and the answer/dismiss paths
// that resume it. `hitl` is state (it drives the floating panel). `hitlRef` mirrors it for
// async closures, which read it after an await, when the render's `hitl` is stale.
// `updateHitl` sets both. Every handler is a fresh per-render closure over the options, as
// it was inline in the slot.
export function useHitl({ sessionId, runTurn, noteToThread, lastAssistantId }: UseHitlOptions) {
  const [hitl, setHitlState] = useState<HitlPayload | null>(null);
  // Ref mirror for async closures (reconcileSteer runs after an await — the render
  // closure's `hitl` is stale by then). Always set both via updateHitl.
  const hitlRef = useRef<HitlPayload | null>(null);
  const updateHitl = (payload: HitlPayload | null) => {
    hitlRef.current = payload;
    setHitlState(payload);
  };

  // Resume a paused (input-required) turn: submitting the HITL form/question
  // sends the response as a follow-up on the same session — the server feeds it
  // to the agent via Command(resume=…). A form response is serialized to JSON.
  // Redeem a plugin composer-form (#1701 Slice 2): POST the field values to the plugin's
  // on_submit. A reply becomes a note; a returned form is the next step of a wizard
  // (re-opened on the same input-required HITL path).
  async function submitPluginForm(callbackId: string, answers: Record<string, unknown>) {
    try {
      const res = await api.submitChatCommandForm({
        callback_id: callbackId,
        session_id: sessionId ?? "",
        answers,
      });
      if (res?.form) {
        updateHitl({ ...res.form, plugin_callback_id: res.callback_id });
      } else if (res?.reply) {
        noteToThread(String(res.reply));
      }
    } catch (e) {
      noteToThread(`⚠️ ${e instanceof Error ? e.message : String(e)}`, { tone: "danger" });
    }
  }

  async function resumeHitl(response: Record<string, unknown> | string) {
    // A plugin composer-form (#1701 Slice 2) rode the input_required frame but is NOT a
    // graph interrupt — redeem it via the plugin submit route, never Command(resume).
    if (hitl?.plugin_callback_id) {
      const cb = hitl.plugin_callback_id;
      updateHitl(null);
      await submitPluginForm(cb, typeof response === "string" ? {} : response);
      return;
    }
    // An approval gate (Approve/Deny on, e.g., run_command) isn't conversation — resume
    // the turn but DON'T append an "approved"/"denied" user bubble. The outcome lives on
    // the tool card itself (running → done on approve, error on deny), so the bubble is
    // just noise. A form/question answer IS meaningful content, so those stay visible.
    const silent = hitl?.kind === "approval";
    updateHitl(null);
    // For an approval resume, CONTINUE the original assistant message (the one that paused) so the
    // pre- and post-approval tool cards live in ONE bubble / one WorkBlock — otherwise they split
    // across two message bubbles with a gap between them. Forms/questions keep the new-bubble path
    // (their answer is meaningful conversation).
    // `hitlResume` marks this as THE answer to the pending interrupt (#1560): the server
    // resumes the parked graph with it, while any other message sent meanwhile is held
    // and folds in right after.
    void runTurn(
      typeof response === "string" ? response : JSON.stringify(response),
      silent ? { hidden: true, resumeMessageId: lastAssistantId(), hitlResume: true } : { hitlResume: true },
    );
  }

  // Dismiss a paused (input-required) form/question WITHOUT answering it. Clearing the card
  // alone would leave the task parked in input-required forever — that state is exempt from
  // the server TTL sweep, so the LangGraph thread would never settle. Instead RESUME the turn
  // with an explicit "dismissed" sentinel so the agent continues and the task reaches a
  // terminal state. A dismissal isn't conversation content, so resume silently and continue
  // the paused assistant message (matching the approval-resume path) rather than minting a
  // new bubble.
  async function dismissHitl() {
    // A plugin composer-form has no parked graph to resume — just close it; its server
    // callback expires on its own TTL (#1701 Slice 2).
    if (hitl?.plugin_callback_id) {
      updateHitl(null);
      return;
    }
    updateHitl(null);
    void runTurn(
      "[dismissed] The operator dismissed this request without providing input. Continue " +
        "without it — proceed using your best judgment, or stop and explain what you need.",
      { hidden: true, resumeMessageId: lastAssistantId(), hitlResume: true },
    );
  }

  return { hitl, hitlRef, updateHitl, resumeHitl, dismissHitl };
}
