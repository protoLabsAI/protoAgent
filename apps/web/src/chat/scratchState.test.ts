import { beforeEach, describe, expect, it } from "vitest";

import { loadDraft, loadScroll, loadSteers, saveDraft, saveScroll, saveSteers } from "./scratchState";

describe("composer scratch state (Swap & Resume S3)", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("round-trips a draft per session and clears on empty", () => {
    saveDraft("s1", "half-written thought");
    expect(loadDraft("s1")).toBe("half-written thought");
    expect(loadDraft("s2")).toBe(""); // per-session isolation
    saveDraft("s1", "");
    expect(loadDraft("s1")).toBe("");
    expect(window.sessionStorage.length).toBe(0); // empty removes the key
  });

  it("round-trips queued steers and drops malformed entries", () => {
    saveSteers("s1", [{ id: "a", text: "also check the logs" }]);
    expect(loadSteers("s1")).toEqual([{ id: "a", text: "also check the logs" }]);
    window.sessionStorage.setItem("protoagent.chat.steers:host:s1", '[{"bogus":1},{"id":"b","text":"ok"}]');
    expect(loadSteers("s1")).toEqual([{ id: "b", text: "ok" }]);
    saveSteers("s1", []);
    expect(loadSteers("s1")).toEqual([]);
  });

  it("scroll memory: offset round-trips; null means pinned-to-bottom", () => {
    expect(loadScroll("s1")).toBeNull();
    saveScroll("s1", 1234.6);
    expect(loadScroll("s1")).toBe(1235);
    saveScroll("s1", null);
    expect(loadScroll("s1")).toBeNull();
  });
});
