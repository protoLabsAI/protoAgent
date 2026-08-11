// Pure date helpers for the schedule builder's inline calendar (#2159). No Date-picker
// dependency in the DS, so the month grid is hand-rolled — but the fiddly bits (month
// layout, ISO<->parts) live here, pure and unit-tested, away from the component.

/** Split a datetime-local string ("YYYY-MM-DDTHH:mm") into its date + time halves. */
export function splitLocal(local: string): { date: string; time: string } {
  const [date = "", time = ""] = (local || "").split("T");
  return { date, time };
}

/**
 * Compose a datetime-local string from a "YYYY-MM-DD" date and "HH:mm" time.
 * Either half missing → "" (an incomplete schedule, which the builder reports as such).
 *
 * A blank time used to become 09:00 here. That default is why a Windows report of "the
 * Time field didn't take my 23:59" ended with a job actually scheduled for 09:00 and a
 * success message (#2159): whatever swallowed the keystrokes was a UI glitch, but the
 * substitution downstream is what turned it into the wrong job, silently. Missing input
 * is missing — it is never 9am.
 */
export function joinLocal(date: string, time: string): string {
  if (!date || !time) return "";
  return `${date}T${time}`;
}

/**
 * A complete "HH:mm" (24h), or "" for anything partial.
 *
 * `<input type="time">` reports an EMPTY value while its segments are half-filled, so a
 * controlled field sees "" mid-typing. This is the guard for committing a value on blur:
 * take it only when the control has settled on something real, so recovering a swallowed
 * keystroke can never itself write junk into the schedule.
 */
export function completeTime(raw: string): string {
  const m = /^(\d{2}):(\d{2})$/.exec((raw || "").trim());
  if (!m) return "";
  const [h, min] = [Number(m[1]), Number(m[2])];
  return h <= 23 && min <= 59 ? `${m[1]}:${m[2]}` : "";
}

/** Today as "YYYY-MM-DD" in the browser's local zone (never UTC — the user picks local days). */
export function todayISO(now: Date): string {
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** "HH:mm" for `now`, rounded down to the minute — a sensible default when a day is picked. */
export function nowTime(now: Date): string {
  return `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
}

export type Cell = { iso: string; day: number; inMonth: boolean };

/**
 * The 6×7 grid of days for the month containing `viewISO` ("YYYY-MM" or "YYYY-MM-DD"),
 * weeks starting Monday. Leading/trailing cells belong to the neighbouring months
 * (`inMonth: false`) so the grid is always a full rectangle. Pure — no `new Date()` side
 * effects beyond the year/month it's handed.
 */
export function monthGrid(year: number, month0: number): Cell[] {
  const first = new Date(year, month0, 1);
  // JS week starts Sunday (0); shift so Monday is column 0.
  const lead = (first.getDay() + 6) % 7;
  const start = new Date(year, month0, 1 - lead);
  const cells: Cell[] = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    const iso = todayISO(d);
    cells.push({ iso, day: d.getDate(), inMonth: d.getMonth() === month0 });
  }
  return cells;
}

/** Convert "HH:mm" 24h → { h12, minute, ampm } for a 12-hour display. */
export function to12h(time: string): { h12: number; minute: string; ampm: "AM" | "PM" } {
  const [hRaw = "0", m = "00"] = (time || "").split(":");
  const h = Number(hRaw) || 0;
  const ampm = h >= 12 ? "PM" : "AM";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return { h12, minute: m.padStart(2, "0"), ampm };
}

/** { h12 (1–12), minute, ampm } → "HH:mm" 24h. */
export function from12h(h12: number, minute: string, ampm: "AM" | "PM"): string {
  let h = h12 % 12;
  if (ampm === "PM") h += 12;
  return `${String(h).padStart(2, "0")}:${(minute || "00").padStart(2, "0")}`;
}
