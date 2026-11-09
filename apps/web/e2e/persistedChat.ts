import type { Page } from "@playwright/test";

// Saved chat transcripts, read the way the console saves them: from IndexedDB once the
// transcript store is open (epic #4981), else from the localStorage key. Same shape the
// localStorage key had — `{ version, sessions: [{ …, messages }], currentSessionId }` — so a
// spec that asserted on `JSON.parse(localStorage.getItem(key))` keeps its assertions.

export type PersistedChat = {
  version: number;
  sessions: ({ id: string; title?: string; updatedAt: number; messages: Record<string, unknown>[] } & Record<string, unknown>)[];
  currentSessionId: string | null;
};

/** `slug` is the agent ("host" for this instance); `storageKey` overrides the localStorage key. */
export async function persistedChat(page: Page, slug = "host", storageKey?: string): Promise<PersistedChat | null> {
  const key = storageKey ?? (slug === "host" ? "protoagent.chat.sessions" : `protoagent.chat.sessions:${slug}`);
  return page.evaluate(
    async ([slug, key]) => {
      const fromLocal = () => {
        const raw = window.localStorage.getItem(key);
        return raw ? JSON.parse(raw) : null;
      };
      // Never CREATE the database here: an empty one would make the console's own open fail.
      const dbs = (await indexedDB.databases?.()) ?? [];
      if (!dbs.some((d) => d.name === "protoagent-transcripts")) return fromLocal();
      const db = await new Promise<IDBDatabase>((ok, no) => {
        const r = indexedDB.open("protoagent-transcripts");
        r.onsuccess = () => ok(r.result);
        r.onerror = () => no(r.error);
      });
      try {
        if (!db.objectStoreNames.contains("sessions")) return fromLocal();
        const tx = db.transaction(["sessions", "chunks", "agents"], "readonly");
        const get = <T>(r: IDBRequest<T>) => new Promise<T>((ok, no) => { r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error); });
        const range = IDBKeyRange.bound([slug], [slug, []]);
        const records = (await get(tx.objectStore("sessions").getAll(range))) as {
          id: string; updatedAt: number; session: Record<string, unknown>; chunks: string[]; tail: Record<string, unknown>[];
        }[];
        if (!records.length) return fromLocal();
        const chunks = (await get(tx.objectStore("chunks").getAll(range))) as { sessionId: string; hash: string; messages: Record<string, unknown>[] }[];
        const agent = (await get(tx.objectStore("agents").get(slug))) as { order: string[]; currentSessionId: string | null } | undefined;
        const order = new Map((agent?.order ?? []).map((id, i) => [id, i]));
        const sessions = records
          .map((r) => ({
            ...r.session,
            id: r.id,
            updatedAt: r.updatedAt,
            messages: [
              ...r.chunks.flatMap((h) => chunks.find((c) => c.sessionId === r.id && c.hash === h)?.messages ?? []),
              ...r.tail,
            ],
          }))
          .sort((a, b) => (order.get(a.id) ?? 1e9) - (order.get(b.id) ?? 1e9) || b.updatedAt - a.updatedAt);
        return { version: 1, sessions, currentSessionId: agent?.currentSessionId ?? null };
      } finally {
        db.close();
      }
    },
    [slug, key] as const,
  );
}
