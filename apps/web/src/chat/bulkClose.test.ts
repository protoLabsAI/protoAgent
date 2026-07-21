import { describe, expect, it } from "vitest";

import { sessionsToClose } from "./bulkClose";

const sessions = [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }, { id: "e" }];

describe("sessionsToClose", () => {
  it("'others' returns every tab except the anchor", () => {
    expect(sessionsToClose(sessions, "c", "others")).toEqual(["a", "b", "d", "e"]);
  });

  it("'left' returns only the tabs before the anchor, in order", () => {
    expect(sessionsToClose(sessions, "c", "left")).toEqual(["a", "b"]);
  });

  it("'right' returns only the tabs after the anchor, in order", () => {
    expect(sessionsToClose(sessions, "c", "right")).toEqual(["d", "e"]);
  });

  it("'left' on the first tab is empty (nothing to the left)", () => {
    expect(sessionsToClose(sessions, "a", "left")).toEqual([]);
  });

  it("'right' on the last tab is empty (nothing to the right)", () => {
    expect(sessionsToClose(sessions, "e", "right")).toEqual([]);
  });

  it("never includes the anchor itself", () => {
    for (const mode of ["others", "left", "right"] as const) {
      expect(sessionsToClose(sessions, "c", mode)).not.toContain("c");
    }
  });

  it("returns empty for an unknown anchor (nothing to close)", () => {
    expect(sessionsToClose(sessions, "zzz", "others")).toEqual([]);
    expect(sessionsToClose(sessions, "zzz", "left")).toEqual([]);
    expect(sessionsToClose(sessions, "zzz", "right")).toEqual([]);
  });

  it("returns empty for a single-tab strip in every mode", () => {
    const one = [{ id: "only" }];
    expect(sessionsToClose(one, "only", "others")).toEqual([]);
    expect(sessionsToClose(one, "only", "left")).toEqual([]);
    expect(sessionsToClose(one, "only", "right")).toEqual([]);
  });
});
