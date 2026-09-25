import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  canonicalizeRef,
  loadSession,
  normalizeRef,
  RECENT_CAP,
  resetCodeViewer,
  SESSION_KEY,
  setFollow,
  setPinned,
  showCodeRef,
  tidyPath,
  useCodeViewer,
} from "./store";

beforeEach(() => {
  sessionStorage.clear();
  resetCodeViewer();
});

describe("normalizeRef", () => {
  it("needs a project and a path", () => {
    expect(normalizeRef({ project: "", path: "a.ts", source: "link" })).toBeNull();
    expect(normalizeRef({ project: "p", path: "  ", source: "link" })).toBeNull();
  });
  it("floors lines, drops non-positive ones, and keeps endLine ≥ line", () => {
    expect(normalizeRef({ project: "p", path: "a", line: 4.7, endLine: 2, source: "link" })).toMatchObject({
      line: 4,
      endLine: undefined,
    });
    expect(normalizeRef({ project: "p", path: "a", line: 0, endLine: 9, source: "link" })).toMatchObject({
      line: undefined,
      endLine: undefined,
    });
    expect(normalizeRef({ project: "p", path: "a", line: 3, endLine: 8, source: "link" })).toMatchObject({ line: 3, endLine: 8 });
  });
  it("trims the note and drops an empty one", () => {
    expect(normalizeRef({ project: "p", path: "a", note: "  why  ", source: "component" })?.note).toBe("why");
    expect(normalizeRef({ project: "p", path: "a", note: "   ", source: "component" })?.note).toBeUndefined();
  });
});

describe("showCodeRef", () => {
  it("shows the ref on the File tab and bumps seq even for the same ref", () => {
    useCodeViewer.setState({ tab: "diff" });
    showCodeRef({ project: "p", path: "a.ts", line: 3, source: "link" });
    const s1 = useCodeViewer.getState();
    expect(s1.tab).toBe("file");
    expect(s1.current).toMatchObject({ project: "p", path: "a.ts", line: 3 });
    showCodeRef({ project: "p", path: "a.ts", line: 3, source: "link" });
    expect(useCodeViewer.getState().seq).toBe(s1.seq + 1);
  });

  it("keeps a de-duplicated, most-recent-first trail capped at RECENT_CAP", () => {
    for (let i = 1; i <= RECENT_CAP + 5; i++) showCodeRef({ project: "p", path: `f${i}.ts`, source: "link" });
    showCodeRef({ project: "p", path: "f23.ts", source: "link" }); // re-open → moves to the front, once
    const recent = useCodeViewer.getState().recent;
    expect(recent).toHaveLength(RECENT_CAP);
    expect(recent[0].path).toBe("f23.ts");
    expect(recent.filter((r) => r.path === "f23.ts")).toHaveLength(1);
  });

  it("revisiting FROM the trail doesn't reshuffle it", () => {
    showCodeRef({ project: "p", path: "a.ts", source: "link" });
    showCodeRef({ project: "p", path: "b.ts", source: "link" });
    showCodeRef({ project: "p", path: "a.ts", source: "recent" });
    expect(useCodeViewer.getState().recent.map((r) => r.path)).toEqual(["b.ts", "a.ts"]);
    expect(useCodeViewer.getState().current?.path).toBe("a.ts");
  });

  it("ignores an unusable ref", () => {
    expect(showCodeRef({ project: "", path: "", source: "link" })).toBeNull();
    expect(useCodeViewer.getState().seq).toBe(0);
  });
});

describe("follow + pin", () => {
  it("turning follow off drops the pin", () => {
    setFollow(true);
    setPinned(true);
    setFollow(false);
    expect(useCodeViewer.getState()).toMatchObject({ follow: false, pinned: false });
  });
});

describe("one file, one Recent entry (canonical paths)", () => {
  it("tidies ./ and doubled slashes client-side", () => {
    expect(tidyPath("./src//x.ts")).toBe("src/x.ts");
    expect(tidyPath("src/./x.ts")).toBe("src/x.ts");
    expect(tidyPath("/etc/passwd")).toBe("/etc/passwd"); // the server refuses it, not us
    showCodeRef({ project: "p", path: "./src/x.ts", source: "link" });
    showCodeRef({ project: "p", path: "src/x.ts", source: "link" });
    expect(useCodeViewer.getState().recent).toHaveLength(1);
  });
  it("adopts the server's canonical path and folds the duplicate", () => {
    showCodeRef({ project: "p", path: "lib/x.ts", source: "link" });
    showCodeRef({ project: "p", path: "link-to-lib/x.ts", source: "link" });
    canonicalizeRef("p", "link-to-lib/x.ts", "lib/x.ts");
    const s = useCodeViewer.getState();
    expect(s.current?.path).toBe("lib/x.ts");
    expect(s.recent.map((r) => r.path)).toEqual(["lib/x.ts"]);
  });
});

describe("session restore", () => {
  it("round-trips current + recent through sessionStorage", () => {
    showCodeRef({ project: "p", path: "a.ts", line: 4, note: "why", source: "component" });
    const back = loadSession();
    expect(back.current).toMatchObject({ project: "p", path: "a.ts", line: 4, note: "why" });
    expect(back.recent).toHaveLength(1);
  });
  it("survives garbage and storage that throws", () => {
    sessionStorage.setItem(SESSION_KEY, "{not json");
    expect(loadSession()).toEqual({ current: null, recent: [] });
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(loadSession()).toEqual({ current: null, recent: [] });
    spy.mockRestore();
  });
});
