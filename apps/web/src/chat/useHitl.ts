import { useRef, useState } from "react";

import { api } from "../lib/api";
import type {
  FormResponseDisplay,
  FormResponseField,
  FormResponseGroup,
  HitlPayload,
  SystemNoteTone,
} from "../lib/types";
import { fieldsOf, isMultiChoice, optionsOf, type FieldSchema } from "./hitl-form";

/** The slot's `runTurn` signature, narrowed to the options a HITL answer sends. `formResponse`
 *  is the presentation-only label map (#4259): it rides the resume turn so the user bubble can
 *  render a card, and never alters `content` (the byte-identical resume JSON). */
export type HitlRunTurn = (
  content: string,
  opts?: {
    hidden?: boolean;
    resumeMessageId?: string;
    hitlResume?: boolean;
    formResponse?: FormResponseDisplay;
  },
) => Promise<void>;

const EM_DASH = "—";

/** Human display string for one submitted field value, honoring the field's JSON Schema:
 *  `oneOf`/`enum` option titles (single-select and multi-select arrays), "Yes"/"No" for a
 *  boolean, an em dash for an empty/skipped answer, and `String()` for everything else. Pure —
 *  derives ONLY from the schema + the value, never from the resume JSON it accompanies, so it
 *  can't change what gets sent (#4259). Exported for direct unit testing. */
export function displayFieldValue(schema: FieldSchema | undefined, value: unknown): string {
  // Multi-select: map each selected value through its option label, joined; empty ⇒ em dash.
  if (schema && isMultiChoice(schema)) {
    const arr = Array.isArray(value) ? value : [];
    if (!arr.length) return EM_DASH;
    const opts = optionsOf(schema);
    return arr.map((v) => opts.find((o) => o.value === String(v))?.label ?? String(v)).join(", ");
  }
  if (schema?.type === "boolean") return value ? "Yes" : "No";
  if (value === undefined || value === null || value === "") return EM_DASH;
  if (Array.isArray(value)) return value.length ? value.map(String).join(", ") : EM_DASH;
  // Single-choice (oneOf cards / enum dropdown): show the option's human title, not its const.
  const opts = schema ? optionsOf(schema) : [];
  return opts.find((o) => o.value === String(value))?.label ?? String(value);
}

/** Derive the PRESENTATION-ONLY label map for a submitted form response (#4259) from the HITL
 *  payload's schema + the answers object. Fields are grouped by wizard step (step title shown
 *  only for multi-step forms), labelled by each field's schema `title` and falling back to the
 *  raw property key when a schema is absent; answer keys with no matching schema field are
 *  appended last, raw-key labelled, so a schema-less or partial form still reads safely.
 *  Returns `undefined` when there is nothing to show (an empty object, a string answer) so the
 *  caller leaves the turn's legacy text untouched. NEVER mutates or re-serializes the answers —
 *  the resume JSON is produced separately and stays byte-identical. */
export function describeFormResponse(
  payload: HitlPayload | null | undefined,
  response: Record<string, unknown> | string,
): FormResponseDisplay | undefined {
  if (!response || typeof response !== "object") return undefined;
  const steps = payload?.steps ?? [];
  // Only a real `request_user_input` form (one with steps) submits an object worth labelling;
  // an `ask_human` question — even one tagged `kind: "form"` — has no steps and submits a plain
  // string, so it keeps its legacy text (and the string-answer guard above covers it anyway).
  if (!steps.length) return undefined;
  const multiStep = steps.length > 1;
  const claimed = new Set<string>();
  const groups: FormResponseGroup[] = [];

  for (const step of steps) {
    const fields: FormResponseField[] = [];
    for (const [key, schema] of fieldsOf(step)) {
      if (!(key in response)) continue;
      claimed.add(key);
      fields.push({ label: schema.title || key, value: displayFieldValue(schema, response[key]) });
    }
    if (fields.length) groups.push(multiStep && step.title ? { title: step.title, fields } : { fields });
  }
  // Answer keys the schema never declared (no schema at all, or a partial one): label each by
  // its raw key so the provenance is still safely readable rather than raw JSON.
  const orphans = Object.keys(response).filter((key) => !claimed.has(key));
  if (orphans.length) {
    groups.push({ fields: orphans.map((key) => ({ label: key, value: displayFieldValue(undefined, response[key]) })) });
  }
  if (!groups.some((group) => group.fields.length)) return undefined;
  return payload?.title ? { title: payload.title, groups } : { groups };
}

/** A submitted form's labelled card as plain text — one `Label: value` line per field, under
 *  its step title when the card groups by step. The send's `display` (the durable transcript's
 *  bubble text), so a chat rebuilt from the server reads like the card, never the resume JSON. */
export function formResponseText(display: FormResponseDisplay): string {
  return display.groups
    .map((group) => [group.title, ...group.fields.map((field) => `${field.label}: ${field.value}`)].filter(Boolean).join("\n"))
    .join("\n\n");
}

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
    // Capture presentation-only labels for a VISIBLE form answer (#4259) BEFORE clearing the
    // payload — the user bubble renders them as a card instead of the raw resume JSON. Only a
    // form/question answer is visible; an approval is silent (no bubble), and a string answer
    // (`ask_human` free text / an approval option) has no schema to label, so both skip it.
    const formResponse = silent ? undefined : describeFormResponse(hitl, response);
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
      silent
        ? { hidden: true, resumeMessageId: lastAssistantId(), hitlResume: true }
        : { hitlResume: true, ...(formResponse ? { formResponse } : {}) },
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
