import { DropdownSelect, Input } from "@protolabsai/ui/forms";
import { Tabs } from "@protolabsai/ui/navigation";
import { AlertTriangle } from "lucide-react";
import { type FocusEvent, useEffect, useMemo, useState } from "react";

import { completeTime, from12h, joinLocal, nowTime, to12h } from "./dateParts";
import { MonthCalendar } from "./MonthCalendar";
import {
  buildOnce,
  buildRepeat,
  cronFieldError,
  describeSchedule,
  isPastOnce,
  localZone,
  type ParsedSchedule,
  type RepeatFreq,
  WEEKDAYS,
} from "./schedule-builder";

// The "when does it run?" builder — Once / Repeat / Cron tabs, a live preview banner, and
// validation. Extracted from the New-schedule modal so the edit dialog offers the SAME
// builder instead of a raw cron box (#2159 parts 3–5). It owns the when-to-run state
// (seeded from `initial`); the host owns the prompt / job-id and reads `onChange`.

type Mode = "once" | "repeat" | "cron";

export type BuilderOut = { schedule: string; timezone?: string; valid: boolean; error: string };

const COMMON_ZONES = [
  "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
  "Europe/London", "Europe/Berlin", "Asia/Tokyo",
];

export function ScheduleBuilder({
  initial,
  onChange,
}: {
  initial: { parsed: ParsedSchedule; timezone: string };
  onChange: (out: BuilderOut) => void;
}) {
  const p = initial.parsed;
  const [mode, setMode] = useState<Mode>(p.mode);
  const [onceDate, setOnceDate] = useState(p.mode === "once" ? p.onceDate : "");
  const [onceTime, setOnceTime] = useState(p.mode === "once" ? p.onceTime : "");
  const [freq, setFreq] = useState<RepeatFreq>(p.mode === "repeat" ? p.freq : "daily");
  const [time, setTime] = useState(p.mode === "repeat" ? p.time : "09:00");
  const [hour12, setHour12] = useState(false);
  const [dow, setDow] = useState(p.mode === "repeat" ? p.dow : 1);
  const [cronRaw, setCronRaw] = useState(p.mode === "cron" ? p.cronRaw : "");
  // Seeded VERBATIM from the host — "" renders as UTC. The New dialog seeds the
  // operator's local zone (#2159 part 5); the edit dialog seeds the job's stored zone,
  // so opening an existing UTC job never silently retimes it to the local zone.
  const [tz, setTz] = useState(initial.timezone);

  // A `type="time"` control reports "" while its segments are half-entered, so a
  // controlled field can end a burst of typing with state still empty if the settled
  // change event doesn't land — the Windows symptom in #2159 (the field shows 23:59,
  // the preview doesn't). Committing again on blur closes that gap from the other end,
  // and `completeTime` means a partial value can never be what gets committed.
  const commitOnBlur = (set: (v: string) => void) => (e: FocusEvent<HTMLInputElement>) => {
    const settled = completeTime(e.currentTarget.value);
    if (settled) set(settled);
  };

  const tzOptions = useMemo(() => {
    const local = localZone();
    // The stored zone joins the list even when it's not a common one, so the dropdown
    // can actually display the job's current value.
    return Array.from(new Set([initial.timezone, local, ...COMMON_ZONES].filter(Boolean)));
  }, [initial.timezone]);

  const schedule = useMemo(() => {
    if (mode === "once") return buildOnce(joinLocal(onceDate, onceTime));
    if (mode === "repeat") return buildRepeat(freq, time, dow);
    return cronRaw.trim();
  }, [mode, onceDate, onceTime, freq, time, dow, cronRaw]);

  // Validation feeds the preview's error line AND gates the host's submit button.
  const error = useMemo(() => {
    if (mode === "once") {
      if (!onceDate) return "Pick a date for the one-off run.";
      // Blank time used to be quietly filled in as 09:00 by joinLocal, so a Time field
      // that lost its input still submitted — as a job at 9am, reported as a success
      // (#2159). Say it instead, and let `valid` gate the host's submit button.
      if (!onceTime) return "Pick a time for the one-off run.";
      if (isPastOnce(schedule)) return "That time is in the past — it won't fire.";
      return "";
    }
    if (mode === "cron") {
      if (!schedule) return "Enter a cron expression.";
      // Field count AND numeric ranges — "60 9 * * *" is five fields but never valid.
      return cronFieldError(schedule);
    }
    // buildRepeat has the same 09:00 fallback. The field is seeded, so this only fires
    // if the control loses its value — which is exactly the case that must not submit.
    if (mode === "repeat" && !time) {
      return freq === "hourly" ? "Pick a minute past the hour." : "Pick a time to repeat at.";
    }
    return "";
  }, [mode, onceDate, onceTime, freq, time, schedule]);

  const valid = !!schedule && !error;
  const timezone = mode !== "once" && tz ? tz : undefined;

  // Report out on any change. onChange is a stable callback from the host.
  useEffect(() => {
    onChange({ schedule, timezone, valid, error });
  }, [schedule, timezone, valid, error, onChange]);

  const preview = describeSchedule(schedule);

  return (
    <div className="schedule-builder">
      {/* Sticky live preview at the top of the form body (#2159 part 3). */}
      <div
        className={`schedule-preview-banner${error ? " has-error" : ""}`}
        data-testid="schedule-preview"
        role="status"
      >
        {schedule ? (
          <div className="preview-line">
            Runs <strong>{preview}</strong>
            <code className="preview-cron">{schedule}</code>
            {mode === "once"
              ? <span className="muted"> · {localZone() || "local time"}</span>
              : tz ? <span className="muted"> · {tz}</span> : null}
          </div>
        ) : error ? null : (
          <span className="muted">Pick when it should run</span>
        )}
        {/* Rendered whether or not a schedule built. An incomplete one produces no
            schedule string at all, and "Pick a time for the one-off run" is a far more
            actionable thing to show there than the generic placeholder (#2159). */}
        {error ? (
          <div className="preview-error" data-testid="schedule-error">
            <AlertTriangle size={13} /> {error}
          </div>
        ) : null}
      </div>

      <Tabs
        ariaLabel="Schedule mode"
        active={mode}
        onSelect={(m) => setMode(m as Mode)}
        items={[
          { id: "once", label: "Once" },
          { id: "repeat", label: "Repeat" },
          { id: "cron", label: "Cron" },
        ]}
      />

      {mode === "once" && (
        <div className="schedule-once">
          <div className="schedule-once-fields">
            <label className="field">
              <span>Date</span>
              <Input type="date" value={onceDate} onChange={(e) => setOnceDate(e.target.value)}
                     data-testid="schedule-once-date" />
            </label>
            <label className="field">
              <span>Time</span>
              <Input type="time" value={onceTime} onChange={(e) => setOnceTime(e.target.value)}
                     onBlur={commitOnBlur(setOnceTime)} data-testid="schedule-once-time" />
            </label>
          </div>
          <MonthCalendar
            selected={onceDate}
            onSelect={(iso) => {
              setOnceDate(iso);
              if (!onceTime) setOnceTime(nowTime(new Date()));
            }}
          />
        </div>
      )}

      {mode === "repeat" && (
        <div className="schedule-repeat">
          <label className="field">
            <span>Frequency</span>
            <DropdownSelect
              id="schedule-freq"
              value={freq}
              onValueChange={(v) => setFreq(v as RepeatFreq)}
              options={[
                { value: "hourly", label: "Every hour" },
                { value: "daily", label: "Every day" },
                { value: "weekdays", label: "Every weekday (Mon–Fri)" },
                { value: "weekly", label: "Every week" },
              ]}
            />
          </label>
          {freq === "weekly" && (
            <label className="field">
              <span>Day</span>
              <DropdownSelect
                value={String(dow)}
                onValueChange={(v) => setDow(Number(v))}
                options={WEEKDAYS.map((d, i) => ({ value: String(i), label: d }))}
              />
            </label>
          )}
          <label className="field">
            <span className="field-label-row">
              {freq === "hourly" ? "Minute" : "Time"}
              {freq !== "hourly" && (
                <button type="button" className="hour-toggle" onClick={() => setHour12((v) => !v)}
                        title="Switch between 24-hour and 12-hour input">
                  {hour12 ? "12h" : "24h"}
                </button>
              )}
            </span>
            {hour12 && freq !== "hourly" ? (
              (() => {
                const { h12, minute, ampm } = to12h(time);
                return (
                  <div className="time-12h" data-testid="schedule-time-12h">
                    <DropdownSelect value={String(h12)} onValueChange={(v) => setTime(from12h(Number(v), minute, ampm))}
                      options={Array.from({ length: 12 }, (_, i) => ({ value: String(i + 1), label: String(i + 1) }))} />
                    <DropdownSelect value={minute} onValueChange={(v) => setTime(from12h(h12, v, ampm))}
                      options={["00", "15", "30", "45"].map((mm) => ({ value: mm, label: mm }))} />
                    <DropdownSelect value={ampm} onValueChange={(v) => setTime(from12h(h12, minute, v as "AM" | "PM"))}
                      options={[{ value: "AM", label: "AM" }, { value: "PM", label: "PM" }]} />
                  </div>
                );
              })()
            ) : (
              <Input type="time" value={time} onChange={(e) => setTime(e.target.value)}
                     onBlur={commitOnBlur(setTime)} data-testid="schedule-time" />
            )}
          </label>
        </div>
      )}

      {mode === "cron" && (
        <label className="field">
          <span>Cron expression (5 fields)</span>
          <Input value={cronRaw} onChange={(e) => setCronRaw(e.target.value)}
                 placeholder='e.g. "0 9 * * 1-5"' data-testid="schedule-cron" />
        </label>
      )}

      {mode !== "once" && (
        <label className="field">
          <span>Timezone</span>
          <DropdownSelect
            id="schedule-tz"
            value={tz}
            onValueChange={(v) => setTz(v)}
            options={[{ value: "", label: "UTC" }, ...tzOptions.map((z) => ({ value: z, label: z }))]}
          />
        </label>
      )}
    </div>
  );
}
