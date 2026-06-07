// Builds the scheduler's `schedule` string from friendly inputs, and describes one
// back in plain English. The backend accepts either a 5-field cron expression
// (recurring) or an ISO-8601 datetime (one-shot) and auto-detects — so the modal
// never makes the operator hand-write cron.

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
