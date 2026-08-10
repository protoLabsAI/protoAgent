// Builds the scheduler's `schedule` string from friendly inputs, and describes one
// back in plain English. The backend accepts either a 5-field cron expression
// (recurring) or an ISO-8601 datetime (one-shot) and auto-detects — so the modal
// never makes the operator hand-write cron.

import { joinLocal } from "./dateParts";

export type RepeatFreq = "hourly" | "daily" | "weekdays" | "weekly";

export const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

/** A `<input type="datetime-local">` value (local wall-clock) → ISO-8601 UTC, for a one-shot fire. */
export function buildOnce(local: string): string {
  if (!local) return "";
  const d = new Date(local);
  if (Number.isNaN(d.getTime())) return "";
  return d.toISOString().replace(/\.\d{3}Z$/, "Z"); // drop milliseconds
}

/** Friendly recurrence → a 5-field cron string. `time` is "HH:MM"; `dow` is 0–6 (Sun–Sat). */
export function buildRepeat(freq: RepeatFreq, time: string, dow: number): string {
  const [h, m] = (time || "09:00").split(":");
  const hh = clamp(parseInt(h, 10), 0, 23, 9);
  const mm = clamp(parseInt(m, 10), 0, 59, 0);
  switch (freq) {
    case "hourly":
      return `${mm} * * * *`;
    case "daily":
      return `${mm} ${hh} * * *`;
    case "weekdays":
      return `${mm} ${hh} * * 1-5`;
    case "weekly":
      return `${mm} ${hh} * * ${clamp(dow, 0, 6, 1)}`;
  }
}

function clamp(n: number, lo: number, hi: number, fallback: number): number {
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : fallback;
}

// Numeric bounds per cron field. Day-of-week allows 7 (croniter's Sunday alias).
const CRON_FIELDS: [label: string, lo: number, hi: number][] = [
  ["minute", 0, 59],
  ["hour", 0, 23],
  ["day-of-month", 1, 31],
  ["month", 1, 12],
  ["day-of-week", 0, 7],
];

/** Validation message for a raw 5-field cron expression, or "" when it's acceptable.
 * Checks the field count AND each field's numeric ranges — `60 9 * * *` must not pass
 * as "five fields present". Fields are `*` or comma lists of `N` / `N-M`, each with an
 * optional `/step`; that covers what the local scheduler evaluates. Named months/days
 * and @macros aren't accepted here — the backend expects numeric cron. */
export function cronFieldError(schedule: string): string {
  const parts = (schedule || "").trim().split(/\s+/);
  if (parts.length !== 5) return "Cron needs exactly 5 fields (min hour dom mon dow).";
  for (let i = 0; i < 5; i++) {
    const [label, lo, hi] = CRON_FIELDS[i];
    for (const atom of parts[i].split(",")) {
      const m = atom.match(/^(\*|\d+(?:-\d+)?)(?:\/(\d+))?$/);
      if (!m) return `Unrecognized ${label} field "${parts[i]}".`;
      if (m[1] !== "*") {
        const [a, b] = m[1].split("-").map(Number);
        if (a < lo || a > hi || (b !== undefined && (b < lo || b > hi || b < a))) {
          return `${label} "${atom}" is out of range (${lo}–${hi}).`;
        }
      }
      if (m[2] !== undefined && Number(m[2]) === 0) {
        return `${label} step "/0" never fires.`;
      }
    }
  }
  return "";
}

/** Plain-English description of a schedule string (cron or ISO). Falls back to the raw string. */
export function describeSchedule(schedule: string): string {
  const s = (schedule || "").trim();
  if (!s) return "";
  if (/^\d{4}-\d{2}-\d{2}T/.test(s)) {
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? "once" : `once — ${d.toLocaleString()}`;
  }
  const parts = s.split(/\s+/);
  if (parts.length !== 5) return s; // custom cron — show as-is
  const [mn, hr, dom, mon, dow] = parts;
  if (hr === "*" && dom === "*" && mon === "*" && dow === "*") {
    return `every hour at :${mn.padStart(2, "0")}`;
  }
  const time = clockTime(hr, mn);
  if (dom === "*" && mon === "*" && time) {
    if (dow === "*") return `every day at ${time}`;
    if (dow === "1-5") return `every weekday at ${time}`;
    if (/^[0-6]$/.test(dow)) return `every ${WEEKDAYS[+dow]} at ${time}`;
  }
  return s; // anything fancier — show the cron
}

function clockTime(hr: string, mn: string): string | null {
  const h = parseInt(hr, 10);
  const m = parseInt(mn, 10);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return null;
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// ── Parsing back the other way (#2159 edit-mode builder) ─────────────────────

export type ParsedSchedule =
  | { mode: "once"; onceDate: string; onceTime: string }
  | { mode: "repeat"; freq: RepeatFreq; time: string; dow: number }
  | { mode: "cron"; cronRaw: string };

/** Inverse of {@link buildOnce}/{@link buildRepeat}: detect which builder mode a stored
 * schedule string came from so the edit dialog can pre-fill the right tab. A cron that
 * doesn't match a friendly preset falls back to raw `cron` mode (never lossy). */
export function parseSchedule(schedule: string): ParsedSchedule {
  const s = (schedule || "").trim();
  // One-shot ISO (UTC) → local date + time halves for the pickers.
  if (/^\d{4}-\d{2}-\d{2}T/.test(s)) {
    const d = new Date(s);
    if (!Number.isNaN(d.getTime())) {
      const p = (n: number) => String(n).padStart(2, "0");
      return {
        mode: "once",
        onceDate: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`,
        onceTime: `${p(d.getHours())}:${p(d.getMinutes())}`,
      };
    }
  }
  const parts = s.split(/\s+/);
  if (parts.length === 5) {
    const [mn, hr, dom, mon, dow] = parts;
    // Only treat a cron as a friendly preset when the numeric fields are plain IN-RANGE
    // integers — an expression like `*/5` or a specific day-of-month is a custom cron,
    // and an out-of-range value ("60 9 * * *") must stay raw too, or the preset
    // round-trip would silently clamp it into a different schedule.
    const time = `${(hr === "*" ? "0" : hr).padStart(2, "0")}:${mn.padStart(2, "0")}`;
    if (/^\d{1,2}$/.test(mn) && Number(mn) <= 59 && dom === "*" && mon === "*") {
      if (hr === "*" && dow === "*") return { mode: "repeat", freq: "hourly", time, dow: 1 };
      if (/^\d{1,2}$/.test(hr) && Number(hr) <= 23) {
        if (dow === "*") return { mode: "repeat", freq: "daily", time, dow: 1 };
        if (dow === "1-5") return { mode: "repeat", freq: "weekdays", time, dow: 1 };
        if (/^[0-6]$/.test(dow)) return { mode: "repeat", freq: "weekly", time, dow: Number(dow) };
      }
    }
  }
  return { mode: "cron", cronRaw: s };
}

/** The builder's normalized form of a stored schedule string: parse → rebuild. A friendly
 * preset with cosmetic differences (a leading-zero hour like "0 09 * * *") canonicalizes
 * to what the builder emits ("0 9 * * *"); a custom cron or unparseable string passes
 * through untouched. The edit dialog compares its builder output against THIS, so opening
 * a job the builder merely re-formats doesn't read as an edit. */
export function canonicalSchedule(schedule: string): string {
  const p = parseSchedule(schedule);
  if (p.mode === "once") return buildOnce(joinLocal(p.onceDate, p.onceTime)) || (schedule || "").trim();
  if (p.mode === "repeat") return buildRepeat(p.freq, p.time, p.dow);
  return p.cronRaw;
}

/** True when `schedule` is a one-shot ISO datetime that has already passed — the case
 * that silently never fires. Recurring cron is never "past". */
export function isPastOnce(schedule: string, now: Date = new Date()): boolean {
  const s = (schedule || "").trim();
  if (!/^\d{4}-\d{2}-\d{2}T/.test(s)) return false;
  const d = new Date(s);
  return !Number.isNaN(d.getTime()) && d.getTime() < now.getTime();
}

/** The operator's IANA zone (e.g. "America/Los_Angeles"), or "" if the browser won't say. */
export function localZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}
