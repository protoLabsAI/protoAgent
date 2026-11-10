// Settings → Storage copy that depends on data (docs/design/settings-storage.md). Pure, so the
// sentences the spec fixes can be tested without rendering the panel.

import { bytes } from "../lib/format";
import type { AutoClear } from "../lib/storageEviction";

export const size = (n: number) => bytes(Math.max(0, Math.round(n)));
export const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** "a", "a and b", "a, b and c". */
export function joinAnd(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

const DAY_MS = 24 * 60 * 60 * 1000;

function startOfDay(t: number): number {
  const d = new Date(t);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/** "Today at 14:12", "Yesterday at 09:05", "Nov 7 at 14:12". */
export function whenLabel(at: number, now: number = Date.now()): string {
  const d = new Date(at);
  const time = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const days = Math.round((startOfDay(now) - startOfDay(at)) / DAY_MS);
  if (days <= 0) return `Today at ${time}`;
  if (days === 1) return `Yesterday at ${time}`;
  return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} at ${time}`;
}

/** Was there an automatic clear in the last 24 hours (the status line's "Made room" sentence)? */
export function recentAutoClear(record: AutoClear | null, now: number = Date.now()): boolean {
  return Boolean(record && now - record.at < DAY_MS && record.chats > 0);
}

/** "Made room today at 14:12 by clearing 5 old chats." */
export function madeRoomSentence(record: AutoClear, now: number = Date.now()): string {
  const when = whenLabel(record.at, now);
  return `Made room ${when.charAt(0).toLowerCase()}${when.slice(1)} by clearing ${plural(record.chats, "old chat", "old chats")}.`;
}

/** "Today at 14:12: 5 chats from threeDee (4 chats) and promptInjection (1 chat), which are no
 *  longer in your fleet. 302 KB freed." */
export function lastClearSentence(record: AutoClear, nameOf: (slug: string) => string, now: number = Date.now()): string {
  const groups = record.agents.length + (record.quick ? 1 : 0);
  let what: string;
  if (!record.agents.length) {
    what = plural(record.quick, "quick chat thread", "quick chat threads");
  } else {
    const parts = record.agents.map((a) => (groups > 1 ? `${nameOf(a.slug)} (${plural(a.chats, "chat", "chats")})` : nameOf(a.slug)));
    if (record.quick) parts.push(`quick chats (${plural(record.quick, "thread", "threads")})`);
    const gone = !record.quick && record.agents.every((a) => a.gone);
    const which = gone ? (record.agents.length === 1 ? ", which is no longer in your fleet" : ", which are no longer in your fleet") : "";
    what = `${plural(record.chats, "chat", "chats")} from ${joinAnd(parts)}${which}`;
  }
  return `${whenLabel(record.at, now)}: ${what}. ${size(record.bytes)} freed.`;
}

/** "Last saved 3 days ago" (the row tooltip). */
export function lastSavedLabel(at: number, now: number = Date.now()): string {
  const s = Math.max(0, (now - at) / 1000);
  if (s < 60) return "Last saved just now";
  if (s < 3600) return `Last saved ${plural(Math.round(s / 60), "minute", "minutes")} ago`;
  if (s < 86400) return `Last saved ${plural(Math.round(s / 3600), "hour", "hours")} ago`;
  return `Last saved ${plural(Math.round(s / 86400), "day", "days")} ago`;
}
