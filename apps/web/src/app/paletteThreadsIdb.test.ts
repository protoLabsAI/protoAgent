import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";

// Epic #4981 S3: palette / DM threads keep their messages in IndexedDB once the transcript
// store is open; the localStorage key keeps only the thread's contextId (the server link).

const KEY = "protoagent.palette.chat";
const msgs = (n: number): ChatMessage[] =>
  Array.from({ length: n }, (_, i) => ({ id: `m${i}`, role: i % 2 ? "assistant" : "user", content: `message ${i} ${"x".repeat(2000)}`, status: "done" }) as ChatMessage);

async function fresh() {
  vi.resetModules();
  const palette = await import("./paletteChatStore");
  const { openTranscriptStore } = await import("../chat/transcriptStore");
  return { palette, store: (await openTranscriptStore())! };
}

beforeEach(() => {
  (globalThis as { indexedDB: IDBFactory }).indexedDB = new IDBFactory();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("palette threads in IndexedDB", () => {
  it("migrates a thread's messages out of localStorage, keeping its contextId there", async () => {
    localStorage.setItem(KEY, JSON.stringify({ contextId: "palette-1", messages: msgs(4) }));
    localStorage.setItem(`${KEY}:dm:roxy`, JSON.stringify({ contextId: "palette-dm", messages: msgs(2) }));
    const { palette, store } = await fresh();
    await palette.initPaletteThreads(store);

    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual({ contextId: "palette-1" });
    expect(JSON.parse(localStorage.getItem(`${KEY}:dm:roxy`)!)).toEqual({ contextId: "palette-dm" });
    expect(palette.loadPaletteThread()).toEqual({ contextId: "palette-1", messages: msgs(4) });
    expect(palette.loadPaletteThread("dm:roxy").messages).toHaveLength(2);
    store.close();
  });

  it("saves messages to the store, and a reload reads them back", async () => {
    const first = await fresh();
    await first.palette.initPaletteThreads(first.store);
    const thread = first.palette.freshPaletteThread();
    first.palette.savePaletteThread({ ...thread, messages: msgs(3) }, true);
    await first.palette.paletteWritesSettled();
    // localStorage holds only the link: small, never a >1K overwrite (#4974).
    expect(localStorage.getItem(KEY)!.length).toBeLessThan(100);
    first.store.close();

    const second = await fresh();
    await second.palette.initPaletteThreads(second.store);
    expect(second.palette.loadPaletteThread()).toEqual({ contextId: thread.contextId, messages: msgs(3) });
    second.store.close();
  });

  it("without the store, threads stay whole in localStorage as before", async () => {
    vi.resetModules();
    const palette = await import("./paletteChatStore");
    await palette.initPaletteThreads(null);
    palette.savePaletteThread({ contextId: "c", messages: msgs(1) }, true);
    expect(JSON.parse(localStorage.getItem(KEY)!).messages).toHaveLength(1);
  });
});
