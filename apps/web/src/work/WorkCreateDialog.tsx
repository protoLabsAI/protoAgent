/**
 * New work, steps 1 + 2, and the inspectors' in-place Edit (work-consolidation §10.2–§10.7).
 *
 * Step 1 (S8c): one question — "What should the agent do, and when?" — interpreted by a
 * marked read-only chat turn (§10.6 via S8b) before anything exists to save. Step 2 (S8d):
 * "Check this before saving" — the agent's plain reading, where it lands, and the approved
 * editable fields for its kind. Save calls the primitive's EXISTING API directly
 * (workPersistence) — never a second agent turn. Edit (S8d) opens the SAME step-2 form
 * prefilled from the live item and PATCHes it in place.
 *
 * Phone width means the VIEWPORT, not the surface name: at ≤ 40rem every one of these flows
 * is the bottom `Drawer` on every Work surface, including the rail — never a centered
 * `Dialog` (§10.2).
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button, Callout, Chip } from "@protolabsai/ui/primitives";
import { Spinner } from "@protolabsai/ui/data";
import { Dialog, Drawer } from "@protolabsai/ui/overlays";
import { FormField, Input, Select, Textarea } from "@protolabsai/ui/forms";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { localZone } from "../schedule/schedule-builder";
import type { WorkInterpretation } from "../lib/api/chat";
import type { WorkGroupId } from "./tone";
import {
  draftFromInterpretation,
  draftProblem,
  repeatSentence,
  saveEdit,
  saveWork,
  withRepeat,
  REPEAT_CHOICES,
  type RepeatChoice,
  type SavedWork,
  type WorkDraft,
  type WorkEditTarget,
} from "./workPersistence";
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

/** ≤ 40rem viewport → bottom sheet (§10.2), live across a resize while the overlay is open. */
function useSheet(): boolean {
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
  return sheet;
}

/** Give focus back to the element that opened the overlay — the DS Dialog/Drawer never
 *  restore it on close (same gap SurfaceHost works around). Read on the FIRST render,
 *  before the DS focus trap moves focus inside. */
function useFocusReturn() {
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
}

/** The shared overlay host: the §10.2 bottom sheet at phone width, the dialog otherwise. */
function WorkOverlay({ sheet, title, onClose, footer, children }: {
  sheet: boolean; title: string; onClose: () => void; footer: ReactNode; children: ReactNode;
}) {
  const className = `work-create${sheet ? " work-create--sheet" : ""}`;
  return sheet
    ? <Drawer open side="bottom" title={title} onClose={onClose} footer={footer} className={className}>{children}</Drawer>
    : <Dialog open title={title} onClose={onClose} footer={footer} className={className}>{children}</Dialog>;
}

/** The registered plugin checks a Condition's "Check with" may pick from (§10.4) — the live
 *  server registry, never a hardcoded copy. Until it loads (or if it fails), the current
 *  check stays the only offer, so the picker can't offer an unregistered check. */
function useConditionChecks(enabled: boolean): string[] {
  const [checks, setChecks] = useState<string[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let gone = false;
    api.verifiers().then((catalog) => {
      if (!gone) setChecks((catalog.plugin_checks ?? []).map((check) => check.name));
    }).catch(() => {});
    return () => { gone = true; };
  }, [enabled]);
  return checks;
}

/** Who a Job's "Who" may pick (§10.3): this agent, plus every addressable delegate — the
 *  same resolver the chat dispatcher routes with, so Start can't address an unreachable one. */
function useDelegates(enabled: boolean): string[] {
  const [names, setNames] = useState<string[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let gone = false;
    api.chatMentions().then((res) => {
      if (!gone) setNames(res.mentions.map((mention) => mention.name));
    }).catch(() => {});
    return () => { gone = true; };
  }, [enabled]);
  return names;
}

/** The approved editable fields for one reading (§10.3–§10.6). One component serves the
 *  confirm step and every inspector Edit, so the two can never drift. `original` is the
 *  form's opening draft (the reading / the live item) — the baseline a re-picked check's
 *  args are restored from. */
function DraftFields({ draft, onChange, mode, original }: {
  draft: WorkDraft;
  onChange: (draft: WorkDraft) => void;
  mode: "create" | "edit";
  original?: WorkDraft;
}) {
  const checks = useConditionChecks(draft.kind === "condition");
  const delegates = useDelegates(draft.kind === "job");
  if (draft.kind === "job") {
    return <>
      <FormField label="What">
        <Textarea rows={2} value={draft.what} onChange={(e) => onChange({ ...draft, what: e.target.value })} />
      </FormField>
      <FormField label="Who">
        <Select value={draft.who} onChange={(e) => onChange({ ...draft, who: e.target.value })}>
          {!draft.who && <option value="">Pick who does it…</option>}
          <option value="agent">This agent</option>
          {[...new Set([...(draft.who && draft.who !== "agent" ? [draft.who] : []), ...delegates])].map((name) => (
            <option key={name} value={name}>{name} (delegate)</option>
          ))}
        </Select>
      </FormField>
      <FormField label="Starts" hint="Start runs it as a normal turn in this chat.">
        <p className="work-create__readonly">Now</p>
      </FormField>
    </>;
  }
  if (draft.kind === "time") {
    // A recurring first run is a wall clock in the draft's timezone (workPersistence); say
    // so whenever that zone isn't the viewer's own.
    const zone = draft.timezone && draft.timezone !== localZone() ? `Times are in ${draft.timezone}.` : undefined;
    return <>
      <FormField label="First run" hint={zone}>
        <Input type="datetime-local" value={draft.firstRun}
          onChange={(e) => onChange({ ...draft, firstRun: e.target.value })} />
      </FormField>
      {draft.repeat === "custom" ? (
        // An unmapped repeat never shows cron: the sentence stands in, and the words —
        // not a raw expression — are how the operator changes it (§10.4).
        <FormField label="Repeat" hint={mode === "create"
          ? "To repeat differently, use ← Change the words."
          : "This custom repeat is kept as is. Pick a choice below to replace it."}>
          <p className="work-create__readonly">{repeatSentence(draft)}</p>
        </FormField>
      ) : (
        <FormField label="Repeat">
          {/* withRepeat carries the timezone across the Once/recurring boundary — a kept
              local hour must not silently become a UTC hour, or the reverse (§10.4). */}
          <Select value={draft.repeat} onChange={(e) => onChange(withRepeat(draft, e.target.value as RepeatChoice))}>
            {REPEAT_CHOICES.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
          </Select>
        </FormField>
      )}
      {draft.repeat === "custom" && mode === "edit" && (
        <FormField label="Replace repeat with">
          <Select value="" onChange={(e) => {
            if (e.target.value) onChange({ ...withRepeat(draft, e.target.value as RepeatChoice), cron: "" });
          }}>
            <option value="">Keep as is</option>
            {REPEAT_CHOICES.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
          </Select>
        </FormField>
      )}
      <FormField label="Then">
        <Textarea rows={2} value={draft.then} onChange={(e) => onChange({ ...draft, then: e.target.value })} />
      </FormField>
    </>;
  }
  if (draft.kind === "condition") {
    const offered = [...new Set([...(draft.check ? [draft.check] : []), ...checks])];
    return <>
      <FormField label="When">
        <Input value={draft.when} onChange={(e) => onChange({ ...draft, when: e.target.value })} />
      </FormField>
      <FormField label="Check with">
        <Select value={draft.check} onChange={(e) => {
          const check = e.target.value;
          // Args belong to their check: picking a different one drops them, and picking the
          // ORIGINAL back restores its args — compared against the form's opening draft,
          // not the already-changed current one, so A→B→A round-trips.
          const keep = original?.kind === "condition" ? original : draft;
          onChange({ ...draft, check, checkArgs: check === keep.check ? keep.checkArgs : {} });
        }}>
          {draft.checkKeep && <option value="">Keep the current check ({draft.checkKeep})</option>}
          {offered.map((name) => <option key={name} value={name}>{name}</option>)}
        </Select>
      </FormField>
      <FormField label="Check every" hint="A span like 30m or 2h. Blank uses the instance cadence.">
        <Input value={draft.every} onChange={(e) => onChange({ ...draft, every: e.target.value })} />
      </FormField>
      <FormField label="Then">
        <Textarea rows={2} value={draft.then} onChange={(e) => onChange({ ...draft, then: e.target.value })} />
      </FormField>
      <FormField label="Runs">
        <Select value={draft.each ? "each" : "once"} onChange={(e) => onChange({ ...draft, each: e.target.value === "each" })}>
          <option value="once">Once, then stop</option>
          <option value="each">Each time it becomes true</option>
        </Select>
      </FormField>
      <FormField label="Stop checking after" hint="A span like 6h or 7d. Blank keeps checking.">
        <Input value={draft.stopAfter} onChange={(e) => onChange({ ...draft, stopAfter: e.target.value })} />
      </FormField>
    </>;
  }
  return <>
    <FormField label="Your words" hint={mode === "create"
      ? "Recorded exactly as you typed them. Use ← Change the words to edit them."
      : "The recorded words can't be edited — withdraw this and add a new one to change them."}>
      <blockquote className="work-create__quote">{draft.quote}</blockquote>
    </FormField>
    <FormField label="What it covers">
      <Input value={draft.scope} onChange={(e) => onChange({ ...draft, scope: e.target.value })} />
    </FormField>
    <FormField label="Lasts" hint="Until when, or under what conditions. Blank means until withdrawn.">
      <Input value={draft.lasts} onChange={(e) => onChange({ ...draft, lasts: e.target.value })} />
    </FormField>
    <FormField label="Recorded">
      <p className="work-create__readonly">In this chat</p>
    </FormField>
  </>;
}

export function WorkCreateDialog({ group, sessionId, onClose, onReady, onSaved }: {
  /** The group whose "+" opened this, for the placeholder only — never forces the kind. */
  group?: WorkGroupId;
  /** The current chat (the chat Work was opened from) — the interpret turn stays in it.
   *  Unset (no chat selected / signed out): step 1 still opens — header New shows in every
   *  state — but Continue is gated, because POSTing an empty id would have `/api/chat`
   *  mint a session and the turn would land in a hidden new chat, never the current one. */
  sessionId?: string;
  onClose: () => void;
  /** Observes the step-2 handoff (kept for S8c callers/tests); step 2 renders inline. */
  onReady?: (handoff: WorkCreateHandoff) => void;
  /** A successful save: the caller closes this dialog, opens the new item's group/detail
   *  and announces "Saved to <group>." exactly once (§10.5). */
  onSaved?: (saved: SavedWork) => void;
}) {
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<"ask" | "reading" | "ready">("ask");
  const [error, setError] = useState<string | null>(null);
  const [handoff, setHandoff] = useState<WorkCreateHandoff | null>(null);
  const [draft, setDraft] = useState<WorkDraft | null>(null);
  // The reading's own draft, per confirmed reading — the `original` a re-picked check's
  // args restore from (a ref: it must swap with `nonce` on every new reading, not per render).
  const openingDraft = useRef<WorkDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // One nonce per confirmed reading: a retried Save re-uses the same schedule/watch id, so
  // a create that actually landed before the error can never be duplicated (§10.5).
  const nonce = useRef<string>("");
  // DS Textarea takes no ref (plain TextareaHTMLAttributes) — reach the box via the body.
  const bodyRef = useRef<HTMLDivElement>(null);
  const focusBox = () => bodyRef.current?.querySelector("textarea")?.focus();
  const sheet = useSheet();
  // The element that opened the dialog — Escape/Cancel give focus back to it.
  useFocusReturn();
  // Focus starts in the text box (§10.7). This parent effect runs after the DS trap's
  // own child effect has focused the first focusable (the × close), so the box wins —
  // and again when Change the words returns to step 1, or a resize swaps the host.
  useEffect(() => { if (phase === "ask") focusBox(); }, [phase, sheet]);

  const reading = phase === "reading";
  // The chat the one marked turn goes to. Only a real id is ever posted.
  const chat = sessionId?.trim() ? sessionId : undefined;
  const send = async () => {
    if (!chat) return; // Continue is gated on `chat`; belt for a programmatic click.
    const typed = text;
    setPhase("reading");
    setError(null);
    try {
      const result = await api.interpretWork(typed, chat);
      let interpretation = result.messages?.find((m) => m.interpretation)?.interpretation;
      if (!interpretation) {
        // Desktop/proxy fallbacks may flatten the turn to its joined text — that text IS
        // the reading's JSON. Anything unparseable falls through to the readable error.
        try { interpretation = JSON.parse(result.response) as WorkInterpretation; } catch { interpretation = undefined; }
      }
      if (!interpretation?.says || !interpretation.kind) throw new Error("The agent's reply was not a reading.");
      // BEFORE anything shows or saves: the operator's typed bytes replace the model's
      // copy of the quote — never trusted, never trimmed (§10.6).
      if (interpretation.instruction) interpretation.instruction.quote = typed;
      const next = { typed, interpretation, sessionId: chat };
      nonce.current = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
      const first = draftFromInterpretation(typed, interpretation);
      openingDraft.current = first;
      setHandoff(next);
      setDraft(first);
      setSaveError(null);
      setPhase("ready");
      onReady?.(next);
    } catch (e) {
      // The typed text is untouched: the box is editable again and Continue retries.
      setError(errMsg(e));
      setPhase("ask");
    }
  };

  const saved = useRef(false);
  // A ref, not the `saving` state: two clicks in the same tick both read the stale state,
  // and a double-clicked Save must be ONE save (§10.5).
  const inFlight = useRef(false);
  const save = async () => {
    if (!chat || !draft || inFlight.current || saved.current) return;
    inFlight.current = true;
    setSaving(true);
    setSaveError(null);
    try {
      const result = await saveWork(draft, chat, nonce.current);
      saved.current = true; // double-fire belt; the caller closes this dialog next
      onSaved?.(result);
    } catch (e) {
      // Everything typed and edited is retained; Save tries again with the same ids.
      setSaveError(errMsg(e));
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  };

  const ask = phase !== "ready";
  const problem = handoff?.interpretation.problem ?? null;
  const incomplete = draft ? draftProblem(draft) : problem ? null : "The reading had nothing to save.";
  const body = ask ? (
    <div className="work-create__body" ref={bodyRef}>
      {!chat && <Callout tone="warning" title="No chat is open">
        The agent reads your words in the current chat. Open a chat, then start New again.
      </Callout>}
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
      <Callout tone={problem ? "warning" : "info"}>
        {problem ?? handoff!.interpretation.says}
      </Callout>
      {!problem && <p className="work-create__dest">
        Goes in <strong>{DESTINATIONS[handoff!.interpretation.group]}</strong> as {tagFor(handoff!.interpretation)}.
      </p>}
      {!problem && draft && <DraftFields draft={draft} onChange={setDraft} mode="create" original={openingDraft.current ?? undefined} />}
      {!problem && incomplete && <p className="work-create__incomplete" role="status">{incomplete}</p>}
      {saveError != null && <Callout tone="error" title="Couldn't save it">
        {saveError} Everything you entered is kept — Save tries again without creating a duplicate.
      </Callout>}
    </div>
  );

  const size = sheet ? ("md" as const) : ("sm" as const);
  const back = <Button size={size} disabled={saving} onClick={() => setPhase("ask")}>← Change the words</Button>;
  const cancel = <Button size={size} disabled={saving} onClick={onClose}>Cancel</Button>;
  const confirm = (
    <Button size={size} variant="primary" disabled={!text.trim() || reading || !chat} loading={reading} onClick={send}>
      Continue
    </Button>
  );
  const saveButton = (
    <Button size={size} variant="primary" disabled={!!problem || !!incomplete || saving} loading={saving} onClick={() => void save()}>
      {draft?.kind === "job" ? "Start" : "Save"}
    </Button>
  );
  // Visual and reading order match (§10.7): the sheet stacks the primary FIRST in the
  // DOM, the dialog keeps the usual Cancel-then-primary row. A problem reading has no
  // primary at all — nothing unreadable may be executable.
  const footer = ask
    ? (sheet ? <>{confirm}{cancel}</> : <>{cancel}{confirm}</>)
    : problem
      ? (sheet ? <>{back}{cancel}</> : <>{cancel}{back}</>)
      : (sheet ? <>{saveButton}{back}{cancel}</> : <>{cancel}{back}{saveButton}</>);
  const title = ask ? "New" : "Check this before saving";
  return <WorkOverlay sheet={sheet} title={title} onClose={onClose} footer={footer}>{body}</WorkOverlay>;
}

/**
 * The inspectors' in-place Edit (§10.7): the SAME step-2 form, prefilled from the live item —
 * no interpret turn, no model call. Saving PATCHes the existing primitive: a condition keeps
 * its watch id, history and check count; an instruction's quote is immutable.
 */
export function WorkEditDialog({ draft: initial, target, onClose, onSaved }: {
  draft: WorkDraft;
  target: WorkEditTarget;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  const base = useMemo(() => initial, []); // eslint-disable-line react-hooks/exhaustive-deps -- the open-time snapshot IS the baseline
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const sheet = useSheet();
  useFocusReturn();
  const incomplete = draftProblem(draft, "edit");

  // Same in-flight ref as the create save: a same-tick double click reads stale state.
  const inFlight = useRef(false);
  const save = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    setSaveError(null);
    try {
      await saveEdit(draft, base, target);
      onSaved?.();
      onClose();
    } catch (e) {
      setSaveError(errMsg(e));
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  };

  const size = sheet ? ("md" as const) : ("sm" as const);
  const cancel = <Button size={size} disabled={saving} onClick={onClose}>Cancel</Button>;
  const saveButton = (
    <Button size={size} variant="primary" disabled={!!incomplete || saving} loading={saving} onClick={() => void save()}>
      Save changes
    </Button>
  );
  const footer = sheet ? <>{saveButton}{cancel}</> : <>{cancel}{saveButton}</>;
  const title = draft.kind === "instruction" ? "Edit instruction" : "Edit follow-up";
  return (
    <WorkOverlay sheet={sheet} title={title} onClose={onClose} footer={footer}>
      <div className="work-create__body">
        <DraftFields draft={draft} onChange={setDraft} mode="edit" original={base} />
        {incomplete && <p className="work-create__incomplete" role="status">{incomplete}</p>}
        {saveError != null && <Callout tone="error" title="Couldn't save the changes">
          {saveError} Nothing changed yet — Save changes tries again.
        </Callout>}
      </div>
    </WorkOverlay>
  );
}
