/**
 * New work, step 1 (work-consolidation §10.2, S8c): one question — "What should the agent
 * do, and when?" — interpreted by a marked read-only chat turn (§10.6 via S8b) before
 * anything exists to save. The agent's reading comes back as a structured handoff for the
 * confirm step (S8d); this step never calls a save or create route, and deliberately ships
 * no Start/Save action — an unread proposal must not be executable.
 *
 * Phone width means the VIEWPORT, not the surface name: at ≤ 40rem the flow is the bottom
 * `Drawer` on every Work surface, including the rail — never a centered `Dialog` (§10.2).
 */
import { useEffect, useRef, useState } from "react";
import { Button, Callout, Chip } from "@protolabsai/ui/primitives";
import { Spinner } from "@protolabsai/ui/data";
import { Dialog, Drawer } from "@protolabsai/ui/overlays";
import { FormField, Textarea } from "@protolabsai/ui/forms";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import type { WorkInterpretation } from "../lib/api/chat";
import type { WorkGroupId } from "./tone";
import "./work-create.css";

/** The §10.2 example chips, verbatim. They fill the box; they never pick a kind. */
export const WORK_CREATE_EXAMPLES = [
  "Check #874 every 30 min and rebase it when #868 merges",
  "Every weekday at 9, summarise what merged overnight",
  "Merge only when the review says PASS",
  "Re-run the design-system audit on protoContent",
] as const;

/** A group "+" seeds only a placeholder (§10.1); the agent's reading still decides the
 *  kind, so nothing here is sent with the turn. Needs you has no entry point at all. */
const GROUP_PLACEHOLDERS: Partial<Record<WorkGroupId, string>> = {
  "doing-now": "What should the agent do now?",
  "coming-up": "What should happen, and when — or on what condition?",
  instructions: "What should the agent always do from now on?",
};

/** Where a reading lands (§10.3): the wire group's public label and kind tag. */
const DESTINATIONS: Record<WorkInterpretation["group"], string> = {
  doing: "Doing now", coming: "Coming up", told: "Your instructions",
};
function tagFor(reading: WorkInterpretation): string {
  if (reading.kind === "job") return "Job";
  if (reading.kind === "instruction") return "Instruction";
  return reading.followup?.trigger === "time" ? "Time" : "Condition";
}

/** The step-2 handoff (S8d): the operator's typed text BYTE FOR BYTE — an instruction's
 *  quote is overwritten with it before anything shows, so a model slip can never change
 *  the operator's words (§10.6) — plus the validated reading and the chat it belongs to. */
export type WorkCreateHandoff = {
  typed: string;
  interpretation: WorkInterpretation;
  sessionId: string;
};

export function WorkCreateDialog({ group, sessionId, onClose, onReady }: {
  /** The group whose "+" opened this, for the placeholder only — never forces the kind. */
  group?: WorkGroupId;
  /** The current chat (the chat Work was opened from) — the interpret turn stays in it. */
  sessionId?: string;
  onClose: () => void;
  /** S8d's confirm step takes over from here; without it the reading shows read-only. */
  onReady?: (handoff: WorkCreateHandoff) => void;
}) {
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<"ask" | "reading" | "ready">("ask");
  const [error, setError] = useState<string | null>(null);
  const [handoff, setHandoff] = useState<WorkCreateHandoff | null>(null);
  // DS Textarea takes no ref (plain TextareaHTMLAttributes) — reach the box via the body.
  const bodyRef = useRef<HTMLDivElement>(null);
  const focusBox = () => bodyRef.current?.querySelector("textarea")?.focus();
  // ≤ 40rem viewport → bottom sheet (§10.2), live across a resize while open.
  const [sheet, setSheet] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(max-width: 40rem)").matches);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(max-width: 40rem)");
    const on = () => setSheet(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  // The element that opened the dialog, read during the FIRST render — before the DS
  // focus trap moves focus inside. The DS Dialog/Drawer never restore focus on close
  // (same gap SurfaceHost works around), so Escape/Cancel give it back here.
  const opener = useRef<Element | null | undefined>(undefined);
  if (opener.current === undefined) opener.current = document.activeElement;
  useEffect(() => () => {
    const target = opener.current;
    if (!(target instanceof HTMLElement) || !target.isConnected) return;
    requestAnimationFrame(() => {
      const active = document.activeElement;
      if (target.isConnected && (!active || active === document.body)) target.focus({ preventScroll: true });
    });
  }, []);
  // Focus starts in the text box (§10.7). This parent effect runs after the DS trap's
  // own child effect has focused the first focusable (the × close), so the box wins —
  // and again when Change the words returns to step 1, or a resize swaps the host.
  useEffect(() => { if (phase === "ask") focusBox(); }, [phase, sheet]);

  const reading = phase === "reading";
  const send = async () => {
    const typed = text;
    setPhase("reading");
    setError(null);
    try {
      const result = await api.interpretWork(typed, sessionId ?? "");
      let interpretation = result.messages?.find((m) => m.interpretation)?.interpretation;
      if (!interpretation) {
        // Desktop/proxy fallbacks may flatten the turn to its joined text — that text IS
        // the reading's JSON. Anything unparseable falls through to the readable error.
        try { interpretation = JSON.parse(result.response) as WorkInterpretation; } catch { interpretation = undefined; }
      }
      if (!interpretation?.says || !interpretation.kind) throw new Error("The agent's reply was not a reading.");
      if (interpretation.instruction) interpretation.instruction.quote = typed;
      const next = { typed, interpretation, sessionId: sessionId ?? result.session_id ?? "" };
      setHandoff(next);
      setPhase("ready");
      onReady?.(next);
    } catch (e) {
      // The typed text is untouched: the box is editable again and Continue retries.
      setError(errMsg(e));
      setPhase("ask");
    }
  };

  const ask = phase !== "ready";
  const body = ask ? (
    <div className="work-create__body" ref={bodyRef}>
      <FormField
        label="What should the agent do, and when?"
        hint="Say it in your own words. You'll see how the agent reads it before anything is saved."
      >
        <Textarea
          rows={3}
          value={text}
          readOnly={reading}
          placeholder={group ? GROUP_PLACEHOLDERS[group] : undefined}
          onChange={(event) => setText(event.target.value)}
        />
      </FormField>
      <div className="work-create__examples" role="group" aria-label="Examples">
        {WORK_CREATE_EXAMPLES.map((example) => (
          <Chip key={example} label={example} onClick={() => {
            if (reading) return;
            setText(example);
            focusBox();
          }} />
        ))}
      </div>
      {reading && <div className="work-create__status" role="status"><Spinner size={16} /> Reading what you wrote…</div>}
      {error != null && <Callout tone="error" title="Couldn't read what you wrote">
        {error} Your words are kept — Continue tries again.
      </Callout>}
    </div>
  ) : (
    <div className="work-create__body">
      <Callout tone={handoff!.interpretation.problem ? "warning" : "info"}>
        {handoff!.interpretation.problem ?? handoff!.interpretation.says}
      </Callout>
      {!handoff!.interpretation.problem && <p className="work-create__dest">
        Goes in <strong>{DESTINATIONS[handoff!.interpretation.group]}</strong> as {tagFor(handoff!.interpretation)}.
      </p>}
    </div>
  );

  const back = <Button size={sheet ? "md" : "sm"} onClick={() => setPhase("ask")}>← Change the words</Button>;
  const cancel = <Button size={sheet ? "md" : "sm"} onClick={onClose}>Cancel</Button>;
  const confirm = (
    <Button size={sheet ? "md" : "sm"} variant="primary" disabled={!text.trim() || reading} loading={reading} onClick={send}>
      Continue
    </Button>
  );
  // Visual and reading order match (§10.7): the sheet stacks the primary FIRST in the
  // DOM, the dialog keeps the usual Cancel-then-primary row.
  const footer = ask
    ? (sheet ? <>{confirm}{cancel}</> : <>{cancel}{confirm}</>)
    : (sheet ? <>{back}{cancel}</> : <>{cancel}{back}</>);
  const title = ask ? "New" : "Check this before saving";
  const className = `work-create${sheet ? " work-create--sheet" : ""}`;
  return sheet
    ? <Drawer open side="bottom" title={title} onClose={onClose} footer={footer} className={className}>{body}</Drawer>
    : <Dialog open title={title} onClose={onClose} footer={footer} className={className}>{body}</Dialog>;
}
