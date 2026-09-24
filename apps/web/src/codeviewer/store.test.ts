import { beforeEach, describe, expect, it } from "vitest";

import { normalizeRef, RECENT_CAP, resetCodeViewer, setFollow, setPinned, showCodeRef, useCodeViewer } from "./store";

beforeEach(() => resetCodeViewer());

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
