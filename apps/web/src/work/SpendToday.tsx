import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { Badge, Button, Callout } from "@protolabsai/ui/primitives";
import { Alert } from "@protolabsai/ui/data";
import { api } from "../lib/api";
import type { AnswerResult } from "../lib/api/decisions";
import type { SpendCapStatus } from "../lib/api/telemetry";
import { isConnected, onConnectionChange, onTopic } from "../lib/events";
import "./spend-today.css";

/**
 * "Unattended spend today" — the summary block at the top of Work, above the four groups
 * (docs/design/work-view-additions.md §1, accessibility §4). It reads
 * `GET /api/telemetry/spend-cap` on open, every 60 s while open and on `decision.*` events.
 *
 * The block shows only while the spend cap is on. With the cap off (`enabled: false`), and
 * before the first reading says which it is, the component stays mounted and renders nothing,
 * so a later reading can still show or hide it.
 *
 * A spend-cap pause is ONE agent-wide state, shown here with its decision to answer (§1.5):
 * no job row changes because of it. Resume answers that decision with option `resume`
 * through the caller's normal decision answer path.
 */

const REFRESH_MS = 60_000;
const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const COUNT = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

/** "$6.35", or "unknown" for a reading the meter couldn't take. Never throws. */
export function formatCost(value: number | null | undefined): string {
  return isNumber(value) ? USD.format(value) : "unknown";
}

/** "541,000", or "unknown". Never throws. */
export function formatTokens(value: number | null | undefined): string {
  return isNumber(value) ? COUNT.format(value) : "unknown";
}

/** "Spent so far today: $3.40 and 412,000 tokens." — "unknown" when neither was measured. */
export function formatSpent(cost: number | null | undefined, tokens: number | null | undefined): string {
  if (!isNumber(cost) && !isNumber(tokens)) return "Spent so far today: unknown.";
  return `Spent so far today: ${formatCost(cost)} and ${formatTokens(tokens)} tokens.`;
}

/** "UTC day 2026-11-19 · resets at 00:00 UTC, in 6 h". The day is UTC and says so. */
export function formatDayLine(day: string | null | undefined, now: number): string {
  const date = new Date(now);
  const reset = Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() + 1);
  const minutes = Math.max(1, Math.ceil((reset - now) / 60_000));
  const left = minutes >= 60 ? `${Math.round(minutes / 60)} h` : `${minutes} min`;
  return `UTC day ${day || "unknown"} · resets at 00:00 UTC, in ${left}`;
}

/** "14:02 UTC" — when the reading still showing was taken. */
export function formatReadingTime(at: number): string {
  const date = new Date(at);
  const two = (value: number) => String(value).padStart(2, "0");
  return `${two(date.getUTCHours())}:${two(date.getUTCMinutes())} UTC`;
}

/** A soft (notice) limit counts only when the server sent one: an older server omits the
 *  field, and 0 is the setting's "no notice". Presence decides, never a version check. */
export function noticeLimit(value: number | null | undefined): number | null {
  return isNumber(value) && value > 0 ? value : null;
}

export type SpendState = "cap-off" | "resumed" | "paused" | "running";

/** Read in the spec's order: `enabled` → `approved_today` → `paused` → running (§1.4).
 *  "cap-off" is the hide signal: the block is not shown at all while the cap is off. */
export function spendState(reading: SpendCapStatus): SpendState {
  if (!reading.enabled) return "cap-off";
  if (reading.approved_today) return "resumed";
  if (reading.paused) return "paused";
  return "running";
}

type ShownState = Exclude<SpendState, "cap-off">;

const STATE_WORD: Record<ShownState, string> = {
  resumed: "Resumed for today", paused: "Paused", running: "Running",
};

const STATE_ANNOUNCEMENT: Record<ShownState, string> = {
  resumed: "Unattended work resumed for today.",
  paused: "Unattended work is paused.",
  running: "Unattended work is running.",
};

const reached = (value: number | null | undefined, limit: number | null | undefined) =>
  isNumber(value) && isNumber(limit) && value >= limit;

/** The paused callout's ‹why›, from the reading. With neither limit reached the decision is
 *  still pending after the limits were raised, or it carried over from an earlier UTC day —
 *  so that sentence never says "today". */
export function pausedWhy(reading: SpendCapStatus): string {
  const cost = reached(reading.cost_usd, reading.daily_cost_hard_limit);
  const tokens = reached(reading.total_tokens, reading.daily_token_hard_limit);
  const costLimit = formatCost(reading.daily_cost_hard_limit);
  const tokenLimit = formatTokens(reading.daily_token_hard_limit);
  if (cost && tokens) return `It reached the ${costLimit} and ${tokenLimit}-token daily limits.`;
  if (cost) return `It reached the ${costLimit} daily limit.`;
  if (tokens) return `It reached the ${tokenLimit}-token daily limit.`;
  return "It hit a daily limit and hasn't been resumed yet.";
}

/** "Past today's $5.00 notice limit. …" while running past a notice limit, else null. */
export function pastNoticeLine(reading: SpendCapStatus): string | null {
  const costNotice = noticeLimit(reading.daily_cost_soft_limit);
  const tokenNotice = noticeLimit(reading.daily_token_soft_limit);
  const cost = reached(reading.cost_usd, costNotice);
  const tokens = reached(reading.total_tokens, tokenNotice);
  if (!cost && !tokens) return null;
  const past = cost && tokens
    ? `${formatCost(costNotice)} and ${formatTokens(tokenNotice)}-token notice limits`
    : cost ? `${formatCost(costNotice)} notice limit` : `${formatTokens(tokenNotice)}-token notice limit`;
  return `Past today's ${past}. Unattended work keeps running until ${formatCost(reading.daily_cost_hard_limit)} or ${
    formatTokens(reading.daily_token_hard_limit)} tokens, then pauses.`;
}

type MeterKind = "cost" | "tokens";

/** The §4 `aria-valuetext`: the real value (it may pass 100 %), the daily limit, and the
 *  notice limit only when the response carries one. */
export function meterValueText(kind: MeterKind, value: number | null | undefined, limit: number | null | undefined,
  notice: number | null | undefined): string {
  const format = kind === "cost" ? formatCost : formatTokens;
  const soft = noticeLimit(notice);
  const base = `${format(value)} of ${format(limit)}${kind === "tokens" ? " tokens" : ""} daily limit`;
  if (soft == null) return base;
  return reached(value, soft) ? `${base}, past the notice limit of ${format(soft)}` : `${base}, notice at ${format(soft)}`;
}

/** A hand-built meter from the DS `.pl-progress*` classes: DS `Progress` is a nameless
 *  `role="progressbar"` with no value text and no second mark (DS gap G4). Not focusable. */
function SpendMeter({ kind, label, value, limit, notice, warning }: {
  kind: MeterKind; label: string; value: number | null; limit: number; notice: number | null; warning: boolean;
}) {
  const labelId = useId();
  const format = kind === "cost" ? formatCost : formatTokens;
  const max = isNumber(limit) && limit > 0 ? limit : 0;
  const now = isNumber(value) ? Math.min(Math.max(value, 0), max) : 0;
  // A zero limit is reached by any reading ("Zero pauses immediately"), so the bar is full.
  const percent = max > 0 ? (now / max) * 100 : isNumber(value) ? 100 : 0;
  const passed = reached(value, notice);
  return <div className="pl-progress spend-today__meter" data-meter={kind}>
    <div className="pl-progress__caption">
      <span id={labelId}>{label}</span>
      <span className="pl-progress__value spend-today__value">{format(value)} of {format(limit)}</span>
    </div>
    <div className="spend-today__bar">
      <div className="pl-progress__track" role="meter" aria-labelledby={labelId} aria-valuemin={0} aria-valuemax={max}
        aria-valuenow={now} aria-valuetext={meterValueText(kind, value, limit, notice)}>
        <div className={`pl-progress__fill${warning ? " pl-progress__fill--warning" : ""}`} style={{ width: `${percent}%` }} />
      </div>
      {notice != null && max > 0 && <span className="spend-today__tick" aria-hidden="true"
        style={{ insetInlineStart: `${Math.min(notice / max, 1) * 100}%` }} />}
    </div>
    {notice != null && <span className="pl-chip-meta spend-today__notice">Notice at {format(notice)}{passed ? " · passed" : ""}</span>}
  </div>;
}

type Load = { reading: SpendCapStatus | null; readAt: number | null; failed: boolean };

const NO_DECISION_YET = "A decision to resume appears in Needs you the next time unattended work tries to start.";

export function SpendToday({ answerDecision, onOpenDecision, now = Date.now }: {
  /** The decision's normal answer path (the shared decisions store's `answer`). */
  answerDecision: (id: string, optionId: string) => Promise<AnswerResult>;
  onOpenDecision: (id: string) => void;
  now?: () => number;
}) {
  const headingId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [load, setLoad] = useState<Load>({ reading: null, readAt: null, failed: false });
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const agentOnline = useSyncExternalStore(onConnectionChange, isConnected, isConnected);
  const seq = useRef(0);
  const alive = useRef(true);
  const nowRef = useRef(now);
  nowRef.current = now;
  // This block's own Resume: focus lands on the heading and the badge says so, so the live
  // region stays silent for that one transition (§4).
  const ownResume = useRef(false);
  const focusHeading = useRef(false);
  const previous = useRef<ShownState | null>(null);

  const refresh = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const reading = await api.spendCap();
      // A slower, older read must never replace a newer one.
      if (!alive.current || mine !== seq.current) return;
      setLoad({ reading, readAt: nowRef.current(), failed: false });
    } catch {
      if (!alive.current || mine !== seq.current) return;
      // Stale keeps the last reading; a failed first load has none to keep.
      setLoad((current) => ({ ...current, failed: true }));
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(), REFRESH_MS);
    const offTopic = onTopic("decision.*", () => void refresh());
    return () => {
      alive.current = false;
      clearInterval(timer);
      offTopic();
    };
  }, [refresh]);

  const { reading, readAt, failed } = load;
  const read = reading ? spendState(reading) : null;
  // Cap off: nothing is shown, so there is no state word, live region or Resume.
  const state: ShownState | null = read === "cap-off" ? null : read;
  const hidden = read === "cap-off" || (!reading && !failed);
  useEffect(() => {
    if (read === "cap-off") {
      // The block (and its live region) is gone. When the cap comes back on, the block
      // appearing is not a state change to announce.
      previous.current = null;
      ownResume.current = false;
      setAnnouncement("");
      return;
    }
    if (!state) return;
    if (previous.current && previous.current !== state) {
      if (ownResume.current && state === "resumed") setAnnouncement("");
      else setAnnouncement(STATE_ANNOUNCEMENT[state]);
    }
    ownResume.current = false;
    previous.current = state;
  }, [read, state]);

  const decisionId = reading?.decision_id ?? null;
  const canResume = state === "paused" && !!decisionId;
  useLayoutEffect(() => {
    if (resuming || !focusHeading.current) return;
    focusHeading.current = false;
    // The Resume button is gone, so focus can't stay on it. A Resume that left the pause in
    // place keeps the button, and focus stays there.
    if (!canResume) headingRef.current?.focus({ preventScroll: true });
  }, [resuming, canResume]);

  async function resume() {
    if (!decisionId || resuming) return;
    setResuming(true);
    setResumeError(false);
    try {
      const result = await answerDecision(decisionId, "resume");
      if (!alive.current) return;
      if (result.decision?.state === "answered") {
        // Answered here, or already answered elsewhere: show "Resumed for today" now. The
        // refetch below confirms it, and this holds even if that read fails.
        ownResume.current = true;
        seq.current += 1;
        setLoad((current) => current.reading
          ? { ...current, reading: { ...current.reading, approved_today: true, paused: false } } : current);
      }
      focusHeading.current = true;
      await refresh();
    } catch {
      // The answer didn't land: focus stays on the button and the failure shows beside it.
      if (alive.current) setResumeError(true);
    } finally {
      if (alive.current) setResuming(false);
    }
  }

  const retry = <Button size="sm" onClick={() => void refresh()}>Retry</Button>;
  const hasMeter = !!reading?.meter_available;
  const costNotice = reading ? noticeLimit(reading.daily_cost_soft_limit) : null;
  const tokenNotice = reading ? noticeLimit(reading.daily_token_soft_limit) : null;
  const pastNotice = reading && state === "running" ? pastNoticeLine(reading) : null;
  // Warning fill: the meter at its limit while paused, or past its notice limit while running.
  const warn = (value: number | null, hard: number, notice: number | null) =>
    state === "paused" ? reached(value, hard) : state === "running" && reached(value, notice);

  // Cap off, or no reading yet to say the cap is on: no block at all (§1.4). A failed first
  // load still shows its Alert, because it can't tell whether the cap is on.
  if (hidden) return null;

  return <section className="spend-today" aria-labelledby={headingId} data-state={state ?? "error"}>
    <header className="spend-today__header">
      <h2 className="spend-today__title" id={headingId} ref={headingRef} tabIndex={-1}>Unattended spend today</h2>
      {state && <Badge status="neutral">{STATE_WORD[state]}</Badge>}
    </header>
    {reading && <span className="pl-chip-meta spend-today__day">{formatDayLine(reading.day, now())}</span>}

    {!reading && failed && <Alert status="warning" action={retry}>
      Couldn&apos;t load today&apos;s spend. Unattended work still follows the cap. Only this view failed to load.
    </Alert>}

    {reading && failed && <Callout tone="warning">
      Showing the {formatReadingTime(readAt ?? now())} reading. Couldn&apos;t refresh. {retry}
    </Callout>}

    {reading && state === "resumed" && (hasMeter
      ? <p className="spend-today__text">You resumed unattended work for today. It runs with no daily limit until 00:00 UTC, then the limits apply again.</p>
      : <>
        <p className="spend-today__text">Resumed for today. Spend can&apos;t be measured right now.</p>
        <p className="spend-today__muted">Unattended work runs with no daily limit until 00:00 UTC, then the limits apply again.</p>
      </>)}

    {reading && state === "paused" && <>
      <Callout tone="warning">{hasMeter
        ? `Unattended work is paused for the rest of today (UTC). ${pausedWhy(reading)} Scheduled jobs, watches, inbox and webhook turns won't start. Your own chat turns aren't affected, and job settings are unchanged.`
        : "Unattended work is paused: spend can't be measured right now. The cap fails closed, so nothing unattended starts until the meter is back or you resume for today."}
      </Callout>
      {!hasMeter && <p className="spend-today__text">Spent so far today: unknown.</p>}
      {!decisionId && <p className="spend-today__text">{NO_DECISION_YET}</p>}
    </>}

    {pastNotice && <p className="spend-today__text">{pastNotice}</p>}

    {reading && hasMeter && <div className="spend-today__meters">
      <SpendMeter kind="cost" label="Cost" value={reading.cost_usd} limit={reading.daily_cost_hard_limit}
        notice={costNotice} warning={warn(reading.cost_usd, reading.daily_cost_hard_limit, costNotice)} />
      <SpendMeter kind="tokens" label="Tokens" value={reading.total_tokens} limit={reading.daily_token_hard_limit}
        notice={tokenNotice} warning={warn(reading.total_tokens, reading.daily_token_hard_limit, tokenNotice)} />
    </div>}

    {canResume && decisionId && <div className="spend-today__actions">
      {/* Secondary, not primary: the decision in Needs you is the one primary call to action. */}
      <Button size="sm" disabled={resuming || !agentOnline} onClick={() => void resume()}>Resume unattended work for today</Button>
      <Button size="sm" variant="ghost" onClick={() => onOpenDecision(decisionId)}>Open the decision</Button>
    </div>}
    {canResume && resumeError && <p className="spend-today__text" role="alert">
      Couldn&apos;t send your answer. Nothing changed, and unattended work is still paused.
    </p>}

    {reading && <p className="spend-today__muted">
      Counts turns started by the scheduler, watches, the inbox, webhooks and background jobs. Your own chat turns don&apos;t count.
    </p>}
    <div className="work-view__announcement" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
  </section>;
}
