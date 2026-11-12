/**
 * Direct-save layer for the Work confirm/Edit step (work-consolidation §10.3–§10.7, S8d).
 *
 * Saving NEVER runs a second agent turn: each reading goes straight to the existing API for
 * its primitive — scheduler jobs for a Time follow-up, watches for a Condition follow-up,
 * the directive ledger for an Instruction, and (for a Job) one normal current-chat turn
 * through the chat slot's kickoff seam. The console offers no "task" kind anywhere (§1).
 *
 * Idempotency: a Time/Condition save carries a caller-fixed id (`work-<nonce>`), so a retry
 * after a timed-out create re-uses the SAME id instead of minting a sibling; an Instruction
 * retry first looks for an in-force byte-identical row and re-uses it. Edits PATCH the
 * EXISTING id — a condition edit must never clear + recreate the watch (that would zero its
 * history and check count — the merge-watch-874 regression).
 */
import { api } from "../lib/api";
import { queryClient } from "../lib/queryClient";
import { queryKeys } from "../lib/queries";
import { registerGoalKickoff } from "../chat/chat-store";
import { directiveStore } from "../chat/useDirectives";
import { parseDuration } from "../chat/watchForm";
import { buildOnce, buildRepeat, localZone, parseSchedule } from "../schedule/schedule-builder";
import { scheduleWhen } from "./rowMeta";
import type { DirectiveRow, WorkInterpretation } from "../lib/api/chat";
import type { ScheduledJob, WatchState } from "../lib/types";
import { WORK_GROUP_LABELS, type WorkGroupId } from "./tone";

/** The human Repeat choices (§10.4) — the form never exposes cron. */
export const REPEAT_CHOICES = [
  { value: "once", label: "Once" },
  { value: "30min", label: "Every 30 min" },
  { value: "hourly", label: "Hourly" },
  { value: "daily", label: "Daily" },
  { value: "weekdays", label: "Weekdays" },
  { value: "weekly", label: "Weekly" },
] as const;
export type RepeatChoice = (typeof REPEAT_CHOICES)[number]["value"] | "custom";

/** The editable confirm-form state, one branch per reading (§10.3). Only the fields the
 *  operator may change live here; everything else ("Starts: now", the destination group)
 *  is derived. `firstRun` is a `datetime-local` value (local wall clock). */
export type WorkDraft =
  | { kind: "job"; what: string; who: string }
  | { kind: "time"; firstRun: string; repeat: RepeatChoice; cron: string; then: string; timezone: string }
  | {
      kind: "condition";
      when: string;
      /** A registered plugin check name; "" with `checkKeep` set = keep a non-plugin verifier. */
      check: string;
      checkKeep?: string;
      checkArgs: Record<string, unknown>;
      every: string;
      then: string;
      each: boolean;
      stopAfter: string;
    }
  | { kind: "instruction"; quote: string; scope: string; lasts: string };

export type SavedWork = { group: WorkGroupId; label: string; itemId?: string };

/** "YYYY-MM-DDTHH:mm" (a `datetime-local` value) for a Date, in the viewer's zone. */
function localStamp(date: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}T${p(date.getHours())}:${p(date.getMinutes())}`;
}

/** 1800 → "30m" — the compact duration vocabulary `parseDuration` reads back. */
export function compactDuration(seconds: number): string {
  const s = Math.max(1, Math.round(seconds));
  if (s % 86400 === 0) return `${s / 86400}d`;
  if (s % 3600 === 0) return `${s / 3600}h`;
  if (s % 60 === 0) return `${s / 60}m`;
  return `${s}s`;
}

/** The human choice a stored repeat maps to; anything unmapped is `custom` (sentence only). */
export function repeatChoiceFromCron(repeat: string): RepeatChoice {
  const text = (repeat || "").trim();
  if (text === "once") return "once";
  if (/^\*\/30 \* \* \* \*$/.test(text)) return "30min";
  const parsed = parseSchedule(text);
  if (parsed.mode === "once") return "once";
  if (parsed.mode === "repeat") return parsed.freq;
  return "custom";
}

/** The next calendar date at `time` whose weekday is `dow` — a weekly edit's anchor, so
 *  re-saving an untouched weekly schedule keeps its day instead of sliding to today. */
function nextDow(dow: number, time: string, from: Date = new Date()): string {
  const [h, m] = time.split(":").map((part) => parseInt(part, 10));
  const d = new Date(from);
  d.setHours(Number.isFinite(h) ? h : 9, Number.isFinite(m) ? m : 0, 0, 0);
  while (d.getDay() !== dow || d.getTime() < from.getTime()) d.setDate(d.getDate() + 1);
  return localStamp(d);
}

/** The schedule string a Time draft saves: a one-shot ISO for Once, a 5-field cron derived
 *  from the first run's local time otherwise. A custom draft keeps its original cron. */
export function scheduleFromDraft(draft: Extract<WorkDraft, { kind: "time" }>): string {
  if (draft.repeat === "custom") return draft.cron;
  if (draft.repeat === "once") return buildOnce(draft.firstRun);
  if (draft.repeat === "30min") return "*/30 * * * *";
  const at = new Date(draft.firstRun);
  const valid = !Number.isNaN(at.getTime());
  const time = valid ? localStamp(at).slice(11) : "09:00";
  return buildRepeat(draft.repeat, time, valid ? at.getDay() : 1);
}

/** The plain-words sentence for a custom repeat (§10.4 — shown instead of the select). */
export function repeatSentence(draft: Extract<WorkDraft, { kind: "time" }>): string {
  return scheduleWhen(draft.cron);
}

/** The instant whose wall clock in `zone` reads `stamp` ("YYYY-MM-DDTHH:mm"), or null when
 *  either part is unreadable (the caller then keeps the stamp as typed). Two passes over
 *  the zone's UTC offset settle a stamp near a DST boundary. */
function zonedInstant(stamp: string, zone: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(stamp);
  if (!m) return null;
  const asUtc = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  try {
    const fmt = new Intl.DateTimeFormat("en-US", { timeZone: zone, hour12: false,
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    const wallAsUtc = (t: number) => {
      const p = Object.fromEntries(fmt.formatToParts(new Date(t)).map((part) => [part.type, part.value]));
      return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute);
    };
    const guess = asUtc - (wallAsUtc(asUtc) - asUtc);
    return new Date(guess - (wallAsUtc(guess) - asUtc));
  } catch {
    return null;
  }
}

/** The draft after a Repeat change. A recurring save reads `firstRun` as a wall clock in
 *  `draft.timezone` while a Once save reads it as the VIEWER'S clock, so crossing that
 *  boundary must carry the zone with it: Once → recurring pins the viewer's zone (else the
 *  server reads the kept local hour as UTC), and recurring → Once re-reads the zone's wall
 *  clock into the viewer's before `buildOnce` treats it as local. Staying on one side
 *  changes nothing but the choice. */
export function withRepeat(
  draft: Extract<WorkDraft, { kind: "time" }>, repeat: RepeatChoice,
): Extract<WorkDraft, { kind: "time" }> {
  if (repeat === draft.repeat) return draft;
  if (repeat === "once") {
    const instant = draft.timezone && draft.timezone !== localZone() ? zonedInstant(draft.firstRun, draft.timezone) : null;
    return { ...draft, repeat, firstRun: instant ? localStamp(instant) : draft.firstRun, timezone: "" };
  }
  return draft.repeat === "once" ? { ...draft, repeat, timezone: localZone() } : { ...draft, repeat };
}

/** Build the confirm step's initial draft from a validated reading. The instruction quote
 *  is ALWAYS the operator's typed bytes — never the model's copy (§10.6). */
export function draftFromInterpretation(typed: string, reading: WorkInterpretation): WorkDraft | null {
  if (reading.kind === "job" && reading.job) {
    return { kind: "job", what: reading.job.what, who: reading.job.who === "agent" ? "agent" : "" };
  }
  if (reading.kind === "followup" && reading.followup) {
    const followup = reading.followup;
    if (followup.trigger === "time" && followup.time) {
      const time = followup.time;
      const choice = time.repeat === "once" ? "once" : repeatChoiceFromCron(time.repeat);
      // `firstRun` is a WALL CLOCK in the draft's timezone (the reading's), same as the
      // edit prefill reads a stored job: a recurring cron's fields ARE that wall clock, so
      // deriving the input from the browser-local instant would silently shift the hour.
      // A one-shot has no timezone — its instant converts to the viewer's clock and back.
      const first = new Date(time.first_run);
      const parsed = parseSchedule(time.repeat);
      const firstRun = choice === "once"
        ? (Number.isNaN(first.getTime()) ? "" : localStamp(first))
        : parsed.mode === "repeat"
          ? (parsed.freq === "weekly" ? nextDow(parsed.dow, parsed.time) : `${localStamp(new Date()).slice(0, 10)}T${parsed.time}`)
          : localStamp(new Date());
      return {
        kind: "time",
        firstRun,
        repeat: choice,
        cron: choice === "custom" || choice === "30min" ? time.repeat : "",
        then: followup.then,
        timezone: choice === "once" ? "" : time.timezone || "",
      };
    }
    if (followup.condition) {
      const condition = followup.condition;
      return {
        kind: "condition",
        when: condition.condition,
        check: condition.check,
        checkArgs: condition.check_args ?? {},
        every: condition.interval_s ? compactDuration(condition.interval_s) : "",
        then: followup.then,
        each: condition.repeat,
        stopAfter: condition.expires_in_s ? compactDuration(condition.expires_in_s) : "",
      };
    }
  }
  if (reading.kind === "instruction" && reading.instruction) {
    return { kind: "instruction", quote: typed, scope: reading.instruction.scope, lasts: reading.instruction.conditions };
  }
  return null;
}

/** Prefill for a Time inspector's Edit (§10.7) — never surfaces the raw cron. */
export function draftFromSchedule(job: ScheduledJob): Extract<WorkDraft, { kind: "time" }> {
  const choice = repeatChoiceFromCron(job.schedule);
  const parsed = parseSchedule(job.schedule);
  let firstRun = localStamp(new Date());
  if (parsed.mode === "once") firstRun = `${parsed.onceDate}T${parsed.onceTime}`;
  else if (parsed.mode === "repeat") {
    firstRun = parsed.freq === "weekly"
      ? nextDow(parsed.dow, parsed.time)
      : `${localStamp(new Date()).slice(0, 10)}T${parsed.time}`;
  }
  return { kind: "time", firstRun, repeat: choice, cron: choice === "custom" || choice === "30min" ? job.schedule : "",
    then: job.prompt, timezone: job.timezone ?? "" };
}

/** Prefill for a Condition inspector's Edit. A non-plugin verifier (an operator-created
 *  command/ci/data watch) can't be re-expressed by the §10.4 check picker, so it shows as
 *  "keep the current check" and the PATCH never touches the verifier unless re-picked. */
export function draftFromWatch(watch: WatchState): Extract<WorkDraft, { kind: "condition" }> {
  const verifier = watch.verifier ?? {};
  const plugin = verifier.type === "plugin";
  const remaining = watch.deadline != null ? watch.deadline - Date.now() / 1000 : null;
  return {
    kind: "condition",
    when: watch.condition,
    check: plugin ? String((verifier as { check?: unknown }).check ?? "") : "",
    ...(plugin ? {} : { checkKeep: String(verifier.type ?? "llm") }),
    checkArgs: plugin && typeof (verifier as { args?: unknown }).args === "object" && (verifier as { args?: unknown }).args !== null
      ? ((verifier as { args: Record<string, unknown> }).args)
      : {},
    every: watch.interval_s ? compactDuration(watch.interval_s) : "",
    then: watch.run_prompt ?? "",
    each: watch.repeat === true,
    stopAfter: remaining != null && remaining > 0 ? compactDuration(remaining) : "",
  };
}

/** Prefill for an Instruction/Approval inspector's Edit. The quote is immutable (§10.6). */
export function draftFromDirective(row: DirectiveRow): Extract<WorkDraft, { kind: "instruction" }> {
  return { kind: "instruction", quote: row.quote, scope: row.scope, lasts: row.conditions };
}

/** Why the draft can't be saved yet, or null. An incomplete or unparseable field disables
 *  Save (§10.3) — the message is the FormField-level hint the form shows. A condition's
 *  "Then" is required only on CREATE (the §10.4 reading always carries one): an existing
 *  watch may legitimately record trips with no reaction, and Edit must not strand it. */
export function draftProblem(draft: WorkDraft, mode: "create" | "edit" = "create"): string | null {
  switch (draft.kind) {
    case "job":
      if (!draft.what.trim()) return "Say what the agent should do.";
      if (!draft.who) return "Pick who should do it.";
      return null;
    case "time":
      if (draft.repeat !== "custom" && Number.isNaN(new Date(draft.firstRun).getTime())) return "Pick a first run time.";
      if (!draft.then.trim()) return "Say what should happen.";
      return null;
    case "condition":
      if (!draft.when.trim()) return "Say what to wait for.";
      if (!draft.check && !draft.checkKeep) return "Pick a check.";
      if (draft.every.trim() && !parseDuration(draft.every)) return "Check every needs a span like 30m or 2h.";
      if (draft.stopAfter.trim() && !parseDuration(draft.stopAfter)) return "Stop checking after needs a span like 6h or 7d.";
      if (mode === "create" && !draft.then.trim()) return "Say what should happen when it's met.";
      return null;
    case "instruction":
      if (!draft.quote) return "There are no words to record.";
      if (mode === "create" && !draft.scope.trim()) return "Say what it covers.";
      return null;
  }
}

/** The §10.4 plugin-verifier dict a Condition draft saves. Args follow their check: an
 *  unchanged check keeps them, a re-picked one drops them (they belong to the old check). */
function conditionVerifier(draft: Extract<WorkDraft, { kind: "condition" }>): Record<string, unknown> {
  return { type: "plugin", check: draft.check, ...(Object.keys(draft.checkArgs).length ? { args: draft.checkArgs } : {}) };
}

/**
 * Create the draft's primitive through its EXISTING API, in the current chat's context.
 * `nonce` is fixed per form instance: Save-again-after-an-error re-uses the same ids, so a
 * create that actually landed (a timeout, a double click) is never duplicated.
 */
export async function saveWork(draft: WorkDraft, sessionId: string, nonce: string): Promise<SavedWork> {
  switch (draft.kind) {
    case "job": {
      // One NORMAL current-chat turn (visible user bubble): the agent's job starts as
      // "Start: <what>"; a chosen delegate rides the chat's own @-mention dispatch, which
      // runs it as a background delegation. No new primitive, no second interpret turn.
      const prompt = draft.who === "agent" ? `Start: ${draft.what}` : `@${draft.who} Start: ${draft.what}`;
      registerGoalKickoff(sessionId, prompt, { hidden: false });
      return { group: "doing-now", label: WORK_GROUP_LABELS["doing-now"] };
    }
    case "time": {
      const id = `work-${nonce}`;
      const once = draft.repeat === "once" || (draft.repeat === "custom" && parseSchedule(draft.cron).mode === "once");
      let job: ScheduledJob;
      try {
        job = (await api.addSchedule({
          prompt: draft.then,
          schedule: scheduleFromDraft(draft),
          job_id: id,
          // Mirrors the agent's schedule_task: the owner chat always, the run context only
          // for a one-shot (a recurring job runs in its own job-scoped session, ADR 0129).
          origin_session: sessionId,
          ...(once ? { context_id: sessionId } : {}),
          ...(draft.repeat !== "once" && draft.timezone ? { timezone: draft.timezone } : {}),
        })).job;
      } catch (e) {
        // A retry after a create that LANDED (a timeout, a dropped response) re-POSTs the
        // same job_id, which the scheduler's plain insert rejects — the row existing IS the
        // earlier success. Anything else keeps the original error for the form to show.
        const prior = (await api.schedules().catch(() => null))?.jobs.find((row) => row.id === id);
        if (!prior) throw e;
        job = prior;
      }
      // Refetch BEFORE the caller opens the detail: the `scheduler.changed` push usually
      // beats this, but a dropped event must not make the new row read as "gone".
      await queryClient.invalidateQueries({ queryKey: queryKeys.schedules });
      return { group: "coming-up", label: WORK_GROUP_LABELS["coming-up"], itemId: `schedule:${job.id}` };
    }
    case "condition": {
      const id = `work-${nonce}`;
      const interval = parseDuration(draft.every);
      const expires = parseDuration(draft.stopAfter);
      await api.createWatch({
        condition: draft.when,
        verifier: conditionVerifier(draft),
        watch_id: id,
        trigger: "met",
        repeat: draft.each,
        ...(interval ? { interval_s: interval } : {}),
        ...(expires ? { deadline: Math.round(Date.now() / 1000) + expires } : {}),
        run_prompt: draft.then,
        run_session: sessionId,
      });
      await queryClient.invalidateQueries({ queryKey: queryKeys.watches });
      return { group: "coming-up", label: WORK_GROUP_LABELS["coming-up"], itemId: `watch:${id}` };
    }
    case "instruction": {
      // The ledger mints ids server-side, so a retry can't re-use one — instead an in-force
      // row with the SAME content is the earlier create landing. The ledger stores an
      // operator quote byte for byte but TRIMS AND CAPS scope/conditions (MAX_NOTE_CHARS),
      // so those two compare against what the store would have kept, not the raw draft.
      const kept = (s: string) => s.trim().slice(0, 2000);
      const existing = await api.listDirectives(sessionId).catch(() => null);
      const match = existing?.directives.find(
        (row) => row.status === "in_force" && row.quote === draft.quote && row.scope === kept(draft.scope) && row.conditions === kept(draft.lasts),
      );
      const row = match
        ?? (await api.createDirective({ session_id: sessionId, quote: draft.quote, scope: draft.scope, conditions: draft.lasts })).directive;
      // Await the ledger refresh so the caller opens a detail whose row is already listed.
      await directiveStore(sessionId).refresh();
      return { group: "instructions", label: WORK_GROUP_LABELS.instructions, itemId: `directive:${row.id}` };
    }
  }
}

export type WorkEditTarget =
  | { kind: "time"; id: string }
  | { kind: "condition"; id: string }
  | { kind: "instruction"; id: string; sessionId: string };

/**
 * Save an inspector Edit in place (§10.7). A condition PATCHes the ORIGINAL watch id with
 * only the keys that changed against `base` — id, history and check count survive; nothing
 * is ever cleared or recreated. An instruction edits scope/conditions only (the quote has
 * no write path at all).
 */
export async function saveEdit(draft: WorkDraft, base: WorkDraft, target: WorkEditTarget): Promise<void> {
  if (draft.kind === "time" && target.kind === "time") {
    await api.updateSchedule(target.id, {
      prompt: draft.then,
      schedule: scheduleFromDraft(draft),
      // The console always sends timezone: null means "back to UTC", not "unchanged".
      timezone: draft.repeat === "once" ? null : draft.timezone || null,
    });
    return;
  }
  if (draft.kind === "condition" && target.kind === "condition" && base.kind === "condition") {
    const body: Record<string, unknown> = {};
    if (draft.when !== base.when) body.condition = draft.when;
    if (draft.check && draft.check !== base.check) {
      body.verifier = conditionVerifier({ ...draft, checkArgs: {} });
    }
    if (draft.every !== base.every) body.interval_s = parseDuration(draft.every);
    if (draft.each !== base.each) body.repeat = draft.each;
    if (draft.then !== base.then) body.run_prompt = draft.then;
    if (draft.stopAfter !== base.stopAfter) {
      const expires = parseDuration(draft.stopAfter);
      body.deadline = expires ? Math.round(Date.now() / 1000) + expires : null;
    }
    if (Object.keys(body).length) await api.editWatch(target.id, body);
    return;
  }
  if (draft.kind === "instruction" && target.kind === "instruction") {
    await api.updateDirective(target.sessionId, target.id, { scope: draft.scope, conditions: draft.lasts });
    void directiveStore(target.sessionId).refresh();
    return;
  }
  throw new Error("The edit no longer matches the item.");
}
