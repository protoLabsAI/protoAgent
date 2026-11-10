// Settings → Storage's numbers (docs/design/settings-storage.md §3.2): what this browser keeps
// for this console, by agent, quick chats, and layout and preferences.
//
// Chats come from the transcript store when it is open (sizes are the JSON length of the
// records, an estimate rather than disk use). On the localStorage fallback (the store is null)
// they come from the key registry instead. Every other localStorage key, registered or not
// (plugins, the design system), is counted under layout and preferences.

import type { TranscriptStore } from "../chat/transcriptStore";
import { keySizes, matchKey, readKey } from "../lib/storage";
import { lastWrittenAt } from "../lib/storageEviction";

export type AgentUsage = {
  slug: string;
  chats: number;
  bytes: number;
  /** When a chat of this agent was last saved; `null` = unknown. */
  lastSavedAt: number | null;
  /** What Clear removes: for this window's agent, everything but the chats in use. */
  clearable: { chats: number; bytes: number };
};

export type StorageUsage = {
  backend: "idb" | "local";
  /** Largest first. Only agents with saved chats. */
  agents: AgentUsage[];
  quick: { threads: number; bytes: number };
  /** Layout, preferences, dismissals, sign-in, and every unregistered key. */
  prefsBytes: number;
};

type Totals = { chats: number; bytes: number; lastSavedAt: number | null; clearChats: number; clearBytes: number };

export async function readStorageUsage(opts: {
  store: TranscriptStore | null;
  /** This window's agent ("host" for this instance). */
  here: string;
  /** This window's chats that clearing keeps (open tabs, the focused chat, live replies). */
  protect: ReadonlySet<string>;
}): Promise<StorageUsage> {
  const agents = new Map<string, Totals>();
  const add = (slug: string) => {
    const t = agents.get(slug) ?? { chats: 0, bytes: 0, lastSavedAt: null, clearChats: 0, clearBytes: 0 };
    agents.set(slug, t);
    return t;
  };
  const quick = { threads: 0, bytes: 0 };
  let prefsBytes = 0;

  if (opts.store) {
    const { sessions, threads } = await opts.store.usage();
    for (const s of sessions) {
      const t = add(s.slug);
      t.chats += 1;
      t.bytes += s.bytes;
      t.lastSavedAt = Math.max(t.lastSavedAt ?? 0, s.updatedAt);
      if (s.slug !== opts.here || !opts.protect.has(s.id)) {
        t.clearChats += 1;
        t.clearBytes += s.bytes;
      }
    }
    quick.threads = threads.length;
    quick.bytes = threads.reduce((n, th) => n + th.bytes, 0);
  }

  for (const { key, bytes } of keySizes("local")) {
    const match = matchKey("local", key);
    const id = match?.spec.id;
    const slug = match?.slug ?? "host";
    if (id === "palette.chat") {
      // With the store open the key is only the thread's server link; without it, the thread.
      quick.bytes += bytes;
      if (!opts.store && threadHasMessages(readKey("local", key))) quick.threads += 1;
    } else if (!opts.store && id === "chat.sessions") {
      const t = add(slug);
      t.bytes += bytes;
      const sessions = sessionsOf(readKey("local", key));
      t.chats += sessions.length;
      t.lastSavedAt = lastWrittenAt(readKey("local", key));
      for (const s of sessions) {
        if (slug === opts.here && opts.protect.has(s.id)) continue;
        t.clearChats += 1;
        t.clearBytes += s.bytes;
      }
    } else if (!opts.store && id === "chat.chunk") {
      add(slug).bytes += bytes;
    } else {
      prefsBytes += bytes;
    }
  }

  const rows: AgentUsage[] = [...agents]
    .filter(([, t]) => t.chats > 0)
    .map(([slug, t]) => ({
      slug,
      chats: t.chats,
      bytes: t.bytes,
      lastSavedAt: t.lastSavedAt || null,
      // Another agent's chunks go with it, so its Clear frees the whole row.
      clearable: slug === opts.here ? { chats: t.clearChats, bytes: t.clearBytes } : { chats: t.chats, bytes: t.bytes },
    }))
    .sort((a, b) => b.bytes - a.bytes);
  return { backend: opts.store ? "idb" : "local", agents: rows, quick, prefsBytes };
}

/** A localStorage transcript key's chats, each with its approximate size (UTF-16). */
function sessionsOf(raw: string | null): { id: string; bytes: number }[] {
  try {
    const parsed = JSON.parse(raw ?? "null") as { sessions?: { id?: unknown }[] } | null;
    if (!Array.isArray(parsed?.sessions)) return [];
    return parsed.sessions
      .filter((s) => typeof s?.id === "string")
      .map((s) => ({ id: s.id as string, bytes: 2 * JSON.stringify(s).length }));
  } catch {
    return [];
  }
}

function threadHasMessages(raw: string | null): boolean {
  try {
    const parsed = JSON.parse(raw ?? "null") as { messages?: unknown[] } | null;
    return Array.isArray(parsed?.messages) && parsed.messages.length > 0;
  } catch {
    return false;
  }
}
