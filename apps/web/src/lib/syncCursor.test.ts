import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IDBFactory } from "fake-indexeddb";

import { openTranscriptStore, type TranscriptStore } from "../chat/transcriptStore";
import {
  __resetSyncCursorForTests,
  cursorStorageKey,
  hydrateCursor,
  loadCursor,
  rememberCursor,
  resetCursor,
  setSyncCursorStore,
} from "./syncCursor";

// The live-sync resume cursor (ADR 0130 D3): per-agent, persisted to the synchronous
// sessionStorage mirror and the durable IndexedDB store, read back on reconnect / wake.

describe("syncCursor", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    __resetSyncCursorForTests();
  });
  afterEach(() => {
    window.sessionStorage.clear();
    __resetSyncCursorForTests();
  });

  it("keys the host window un-suffixed and a slug agent by its slug (storage.ts registry)", () => {
    expect(cursorStorageKey("host")).toBe("protoagent.events.since");
    expect(cursorStorageKey("")).toBe("protoagent.events.since");
    expect(cursorStorageKey("roxy")).toBe("protoagent.events.since:roxy");
  });

  it("persists a rendered seq to the mirror and a fresh tab reads it back synchronously", () => {
    rememberCursor("host", 12);
    expect(window.sessionStorage.getItem("protoagent.events.since")).toBe("12");
    __resetSyncCursorForTests(); // forget the in-memory cache — a fresh module reads the mirror
    expect(loadCursor("host")).toBe(12);
  });

  it("is monotonic — a lower or equal seq never rewinds the resume point", () => {
    rememberCursor("roxy", 40);
    rememberCursor("roxy", 7); // a replayed / out-of-order frame
    rememberCursor("roxy", 40);
    expect(loadCursor("roxy")).toBe(40);
    expect(window.sessionStorage.getItem("protoagent.events.since:roxy")).toBe("40");
  });

  it("ignores a non-finite or negative seq", () => {
    rememberCursor("host", Number.NaN);
    rememberCursor("host", -1);
    expect(loadCursor("host")).toBeNull();
  });

  it("resetCursor forces the resume point DOWN past the monotonic guard (the resync_required reset)", () => {
    rememberCursor("roxy", 500);
    // The server resumed us at a LOWER seq (a wiped / foreign log) — the cursor must follow it
    // down, or every reconnect re-triggers resync_required forever.
    resetCursor("roxy", 12);
    expect(loadCursor("roxy")).toBe(12);
    expect(window.sessionStorage.getItem("protoagent.events.since:roxy")).toBe("12");
    // A fresh module (new tab / reload) reads the reset point from the mirror, not the stale 500.
    __resetSyncCursorForTests();
    expect(loadCursor("roxy")).toBe(12);
    // Live frames climb again from there, monotonically.
    rememberCursor("roxy", 13);
    expect(loadCursor("roxy")).toBe(13);
  });

  it("resetCursor ignores a non-finite or negative seq", () => {
    rememberCursor("host", 9);
    resetCursor("host", Number.NaN);
    resetCursor("host", -1);
    expect(loadCursor("host")).toBe(9);
  });

  describe("durable IndexedDB layer", () => {
    let factory: IDBFactory;
    let store: TranscriptStore;
    beforeEach(async () => {
      factory = new IDBFactory();
      store = (await openTranscriptStore({ factory }))!;
      setSyncCursorStore(() => store);
    });
    afterEach(() => store?.close());

    it("writes the cursor through to IndexedDB and a reopened tab resumes from it", async () => {
      rememberCursor("host", 99);
      await vi.waitFor(async () => expect(await store.loadCursor("host")).toBe(99));

      // A reopened tab: no in-memory cache, no per-tab mirror — only the durable store.
      __resetSyncCursorForTests();
      setSyncCursorStore(() => store);
      window.sessionStorage.clear();
      expect(loadCursor("host")).toBeNull(); // nothing synchronous yet
      expect(await hydrateCursor("host")).toBe(99); // folded in from IndexedDB
      expect(loadCursor("host")).toBe(99); // now synchronous, and carried into the mirror
      expect(window.sessionStorage.getItem("protoagent.events.since")).toBe("99");
    });

    it("hydrate keeps the higher of the mirror and the durable store, carrying it back", async () => {
      await store.putCursor("host", 5);
      window.sessionStorage.setItem("protoagent.events.since", "20"); // mirror ahead of the flush
      expect(await hydrateCursor("host")).toBe(20);
      await vi.waitFor(async () => expect(await store.loadCursor("host")).toBe(20));
    });

    it("degrades to the mirror alone when no durable store is registered", async () => {
      __resetSyncCursorForTests(); // provider back to null
      window.sessionStorage.clear();
      rememberCursor("host", 3);
      expect(await hydrateCursor("host")).toBe(3);
    });

    it("resetCursor lowers the durable store too, so a reopened tab resumes from the reset (D3)", async () => {
      rememberCursor("host", 500);
      await vi.waitFor(async () => expect(await store.loadCursor("host")).toBe(500));
      resetCursor("host", 12); // resync_required: the server's authoritative, lower resume point
      await vi.waitFor(async () => expect(await store.loadCursor("host")).toBe(12));

      // A reopened tab reads only the durable store — it must see 12, not the stale high 500,
      // or hydrateCursor's max() would re-raise the cursor and the resync loop would persist.
      __resetSyncCursorForTests();
      setSyncCursorStore(() => store);
      window.sessionStorage.clear();
      expect(await hydrateCursor("host")).toBe(12);
    });
  });
});
