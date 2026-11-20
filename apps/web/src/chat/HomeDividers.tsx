import { useEffect, useRef, useState } from "react";

import type { ChatMessage } from "../lib/types";
import { homeEntryText } from "./HomeAlertCard";

// Home's transcript (#5545, the Home spec) can hold weeks of entries. Two plain-text dividers
// make it skimmable — where each day starts ("Today", "Yesterday", then the date) and where
// the unread run starts ("3 new") — and ONE polite status line tells a screen reader that
// something arrived, by title, instead of the transcript being a live region that would read
// every streamed token.

/** A message that counts as a Home entry for "new": the agent's own message, or a brief /
 *  alert / decision pointer the server wrote into Home's thread (those are stored with the
 *  human role but carry `home` metadata). The operator's own sends never count. */
export function isHomeEntry(message: ChatMessage): boolean {
  return message.role === "assistant" || !!message.home?.kind;
}

/** `last_read_at` (ISO, or null when Home was never read) as epoch ms. Null/empty reads as 0 —
 *  everything is new — matching the Home tab's unread count. */
export function lastReadMs(lastReadAt: string | null | undefined): number {
  return lastReadAt ? Date.parse(lastReadAt) : 0;
}

/** How many Home entries are newer than `lastReadAt` — the Home tab badge and the "N new"
 *  divider share this, so the two can never disagree. */
export function homeUnreadCount(messages: readonly ChatMessage[], lastReadAt: string | null | undefined): number {
  const readAt = lastReadMs(lastReadAt);
  return messages.filter((message) => isHomeEntry(message) && (message.createdAt ?? 0) > readAt).length;
}

function validTime(createdAt: number | undefined): createdAt is number {
  return typeof createdAt === "number" && Number.isFinite(createdAt) && createdAt > 0;
}

const dayKey = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;

/** The divider label of the local calendar day `at` falls on, as seen at `now`. Calendar
 *  days, not 24-hour windows: 23:59 last night is "Yesterday" at 00:01. */
export function dayLabel(at: number, now: number): string {
  const day = new Date(at);
  const today = new Date(now);
  if (dayKey(day) === dayKey(today)) return "Today";
  // The Date constructor normalizes day 0 / month rollover, and stays right across DST.
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (dayKey(day) === dayKey(yesterday)) return "Yesterday";
  return day.toLocaleDateString(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
    ...(day.getFullYear() === today.getFullYear() ? {} : { year: "numeric" }),
  });
}

export type HomeDividerSlot = { day?: string; fresh?: number };

/** What to draw above each message, index-aligned with `messages` (null = nothing). A day
 *  label sits above the first message of each local day; "N new" sits above the first Home
 *  entry newer than `lastReadAt`, counting every such entry. A message with no recorded time
 *  starts no day and is never new. */
export function homeDividers(
  messages: readonly ChatMessage[],
  opts: { lastReadAt: string | null | undefined; now: number },
): (HomeDividerSlot | null)[] {
  const readAt = lastReadMs(opts.lastReadAt);
  const fresh = homeUnreadCount(messages, opts.lastReadAt);
  let lastDay: string | null = null;
  let freshPlaced = false;
  return messages.map((message) => {
    const slot: HomeDividerSlot = {};
    if (validTime(message.createdAt)) {
      const key = dayKey(new Date(message.createdAt));
      if (key !== lastDay) {
        lastDay = key;
        slot.day = dayLabel(message.createdAt, opts.now);
      }
    }
    if (!freshPlaced && fresh > 0 && isHomeEntry(message) && (message.createdAt ?? 0) > readAt) {
      freshPlaced = true;
      slot.fresh = fresh;
    }
    return slot.day !== undefined || slot.fresh !== undefined ? slot : null;
  });
}

/** The current time, re-read when the local day rolls over, so "Today" does not keep naming
 *  yesterday in a console left open overnight. Idle (no timer) while `enabled` is false. */
export function useHomeToday(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const at = new Date();
    const nextMidnight = new Date(at.getFullYear(), at.getMonth(), at.getDate() + 1).getTime();
    // One second past midnight, so a timer that fires a few ms early still lands on the new day.
    const timer = window.setTimeout(() => setNow(Date.now()), Math.max(1000, nextMidnight - at.getTime() + 1000));
    return () => window.clearTimeout(timer);
  }, [enabled, now]);
  return now;
}

/** The `last_read_at` the "N new" divider is measured from. Opening Home marks it read at
 *  once, which would erase the divider before it could be seen — so the value is frozen for
 *  as long as Home stays open, and follows the server again once it is closed. */
export function useHomeNewAnchor(open: boolean, lastReadAt: string | null | undefined): string | null | undefined {
  const frozen = useRef<{ at: string | null | undefined } | null>(null);
  if (!open) frozen.current = null;
  else if (!frozen.current) frozen.current = { at: lastReadAt };
  return frozen.current ? frozen.current.at : lastReadAt;
}

/** A divider line: plain muted text. Deliberately NOT a system message row, a separator role
 *  or the DS divider — it is a label a reader skims past, not a control or a message. */
export function HomeDivider({ slot }: { slot: HomeDividerSlot }) {
  return (
    <>
      {slot.day !== undefined ? <p className="pl-home-divider">{slot.day}</p> : null}
      {slot.fresh !== undefined ? <p className="pl-home-divider pl-home-divider--new">{slot.fresh} new</p> : null}
    </>
  );
}

const TITLE_MAX = 120;

/** The one line that names an entry aloud: a Home entry's title, else the first line of the
 *  agent's message with its markdown markers dropped. */
export function homeEntryTitle(message: ChatMessage): string {
  const raw = message.home?.kind
    ? homeEntryText(message.content).title
    : ((message.content ?? "").split("\n").find((line) => line.trim()) ?? "");
  const title = raw.replace(/^[\s#>*_`-]+/, "").replace(/[*_`]+/g, "").trim();
  if (!title) return "new entry";
  return title.length > TITLE_MAX ? `${title.slice(0, TITLE_MAX - 1).trimEnd()}…` : title;
}

const announceKey = (message: ChatMessage) => message.id || `${message.role}-${message.createdAt}`;

/** Home's announcer: ONE visually hidden polite status line that says "New in Home: <title>"
 *  once for each entry that arrives while Home is open. It never speaks for the operator's own
 *  sends, for entries that were already there when Home was opened (or that load in from
 *  history afterwards), for anything that arrives while Home is closed, or for a message that
 *  is still streaming — that one is named once, when it settles. */
export function HomeAnnouncer({ messages, open }: { messages: readonly ChatMessage[]; open: boolean }) {
  const [text, setText] = useState("");
  const seen = useRef(new Set<string>());
  const said = useRef(new Set<string>());
  const openedAt = useRef<number | null>(null);
  useEffect(() => {
    if (!open) {
      // Closed: everything here now has been "delivered" by the unread badge instead.
      openedAt.current = null;
      said.current.clear();
      setText("");
      for (const message of messages) seen.current.add(announceKey(message));
      return;
    }
    if (openedAt.current === null) openedAt.current = Date.now();
    const titles: string[] = [];
    for (const message of messages) {
      const key = announceKey(message);
      if (seen.current.has(key)) continue;
      // Still streaming: wait. Settling re-stamps `createdAt`, so it is judged then.
      if (message.status === "streaming") continue;
      seen.current.add(key);
      if (!isHomeEntry(message)) continue;
      // Written before Home was opened (or at no recorded time): history, not an arrival.
      if (!validTime(message.createdAt) || message.createdAt < openedAt.current) continue;
      // A settled live reply can be re-read from the server under another id; server-written
      // entries keep stable ids, so only the agent's own messages need this second check.
      if (!message.home?.kind) {
        if (said.current.has(message.content)) continue;
        said.current.add(message.content);
      }
      titles.push(`New in Home: ${homeEntryTitle(message)}`);
    }
    if (!titles.length) return;
    const next = titles.join(". ");
    // An unchanged status line is not re-announced; a trailing no-break space makes a repeat
    // of the same title a real change without changing what is spoken.
    setText((previous) => (previous === next ? `${next} ` : next));
  }, [messages, open]);
  return (
    <span className="pl-home-announce" role="status" aria-live="polite" aria-atomic="true">
      {text}
    </span>
  );
}
