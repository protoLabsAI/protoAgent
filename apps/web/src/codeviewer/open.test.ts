import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useUI } from "../state/uiStore";
import { setCodePaneEnabled } from "./enabled";
import {
  CODE_PANE_WIDTH,
  FOLLOW_THROTTLE_MS,
  followCode,
  followDiff,
  openCode,
  placeCodeSurface,
  resetFollowThrottle,
} from "./open";
import { resetCodeViewer, setFollow, setPinned, useCodeViewer } from "./store";

function setMobile(on: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: on && q.includes("max-width"),
    media: q,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia;
}

const rail = (left: string[], right: string[], bottom: string[] = []) =>
  useUI.setState({ railOrder: { left, right, bottom, hidden: [] } });

beforeEach(() => {
  resetCodeViewer();
  resetFollowThrottle();
  localStorage.clear();
  setMobile(false);
  useUI.setState({ rightWidth: 360, rightCollapsed: true, rightPanel: "work", mobileActive: "chat" });
  rail(["chat", "knowledge"], ["work", "code"]);
  setCodePaneEnabled(true); // the code pane toolset is ON for these suites
});

afterEach(() => {
  vi.useRealTimers();
  setCodePaneEnabled(false);
});

describe("code pane toolset OFF (ADR 0112 amendment)", () => {
  it("openCode and followCode are no-ops — nothing shown, no dock moved or widened", () => {
    setCodePaneEnabled(false);
    rail(["chat", "code"], ["work"]);
    openCode({ project: "app", path: "a.ts", line: 3, source: "link" });
    setFollow(true);
    followCode({ project: "app", path: "b.ts" }, 10_000);
    expect(useCodeViewer.getState().current).toBeNull();
    expect(useUI.getState().railOrder.left).toContain("code");
    expect(useUI.getState().rightWidth).toBe(360);
  });
});

// Josh 2026-09-26: "opening the code link from the artifact view opens the code panel in the
// bottom panel regardless of what rail the code view lives in." The pane opens where the
// operator keeps it; only a surface with NO dock yet is placed (away from chat).
describe("placeCodeSurface — the dock the operator keeps it on", () => {
  const ARTIFACT = "plugin:artifact:artifact";
  const snapshot = () => JSON.stringify(useUI.getState().railOrder);

  it("code on the right with the Artifact panel showing there → opens RIGHT, swapping the diagram out", () => {
    rail(["chat"], ["work", ARTIFACT, "code"]);
    useUI.setState({ rightPanel: ARTIFACT as never, rightCollapsed: false });
    const before = snapshot();
    openCode({ project: "app", path: "src/server.ts", line: 23, source: "link" });
    expect(useUI.getState().rightPanel).toBe("code");
    expect(snapshot()).toBe(before);
  });

  it("code on the bottom → bottom", () => {
    rail(["chat"], ["work", ARTIFACT], ["code"]);
    useUI.setState({ rightPanel: ARTIFACT as never });
    const before = snapshot();
    openCode({ project: "app", path: "a.ts", source: "link" });
    expect(useUI.getState().bottomPanel).toBe("code");
    expect(useUI.getState().rightPanel).toBe(ARTIFACT);
    expect(snapshot()).toBe(before);
  });

  it("code on the left with chat on the right → left", () => {
    rail(["knowledge", "code"], ["chat", "work"]);
    const before = snapshot();
    openCode({ project: "app", path: "a.ts", source: "link" });
    expect(useUI.getState().surface).toBe("code");
    expect(snapshot()).toBe(before);
  });

  it("NEVER mutates railOrder when Code already has a dock — even chat's own", () => {
    const layouts: Array<[string[], string[], string[]]> = [
      [["chat", "knowledge"], ["work", "code"], []],
      [["chat"], ["work"], ["code"]],
      [["knowledge", "code"], ["chat", "work"], []],
      [["knowledge"], ["chat", "work", "code"], []], // on chat's dock: the operator's call
      [["chat", "code"], ["work"], []],
    ];
    for (const [l, r, b] of layouts) {
      for (const shown of ["work", ARTIFACT, "code"]) {
        rail(l, r, b);
        useUI.setState({ rightPanel: shown as never, bottomPanel: shown as never });
        const before = snapshot();
        const dock = placeCodeSurface();
        expect(snapshot()).toBe(before);
        expect(useUI.getState().railOrder[dock]).toContain("code");
      }
    }
  });

  it("puts a hidden surface on the side away from chat", () => {
    useUI.setState({ railOrder: { left: ["chat"], right: ["work"], bottom: [], hidden: ["code"] } });
    expect(placeCodeSurface()).toBe("right");
    expect(useUI.getState().railOrder.right).toContain("code");
    expect(useUI.getState().railOrder.hidden).not.toContain("code");
    useUI.setState({ railOrder: { left: ["knowledge"], right: ["chat", "work"], bottom: [], hidden: ["code"] } });
    expect(placeCodeSurface()).toBe("left");
    expect(useUI.getState().railOrder.left).toContain("code");
  });

  it("puts a MISSING surface on the side away from chat", () => {
    rail(["chat"], ["work"]);
    expect(placeCodeSurface()).toBe("right");
    expect(useUI.getState().railOrder.right).toContain("code");
    rail(["knowledge"], ["chat", "work"]);
    expect(placeCodeSurface()).toBe("left");
  });
});

describe("openCode", () => {
  it("routes to the pane's dock, uncollapsed, and widens the right dock ONCE", () => {
    openCode({ project: "p", path: "a.ts", line: 4, source: "link" });
    const ui = useUI.getState();
    expect(ui.rightCollapsed).toBe(false);
    expect(ui.rightPanel).toBe("code");
    expect(ui.rightWidth).toBe(CODE_PANE_WIDTH);
    // The operator narrows it; the next open must leave their width alone.
    useUI.getState().setRightWidth(300);
    openCode({ project: "p", path: "b.ts", source: "link" });
    expect(useUI.getState().rightWidth).toBe(300);
  });

  it("never narrows a dock that's already wider", () => {
    useUI.setState({ rightWidth: 700 });
    openCode({ project: "p", path: "a.ts", source: "link" });
    expect(useUI.getState().rightWidth).toBe(700);
  });

  it("an AUTO open on mobile only seeds the pane (ADR 0086: nothing takes over the phone)", () => {
    setMobile(true);
    openCode({ project: "p", path: "a.ts", source: "component" }, { auto: true });
    expect(useCodeViewer.getState().current?.path).toBe("a.ts");
    expect(useUI.getState().mobileActive).toBe("chat");
  });

  it("an explicit open on mobile pushes the surface", () => {
    setMobile(true);
    openCode({ project: "p", path: "a.ts", source: "component" });
    expect(useUI.getState().mobileActive).toBe("code");
  });
});

describe("followCode", () => {
  it("does nothing while follow is OFF (the default)", () => {
    followCode({ project: "p", path: "a.ts" }, 10_000);
    expect(useCodeViewer.getState().current).toBeNull();
  });

  it("jumps, then throttles a burst to its LAST ref (trailing)", () => {
    vi.useFakeTimers();
    setFollow(true);
    const t0 = Date.now();
    followCode({ project: "p", path: "a.ts" }, t0);
    expect(useCodeViewer.getState().current?.path).toBe("a.ts");
    followCode({ project: "p", path: "b.ts" }, t0 + 100);
    followCode({ project: "p", path: "c.ts" }, t0 + 200);
    expect(useCodeViewer.getState().current?.path).toBe("a.ts");
    vi.advanceTimersByTime(FOLLOW_THROTTLE_MS);
    expect(useCodeViewer.getState().current?.path).toBe("c.ts");
    expect(useCodeViewer.getState().current?.source).toBe("follow");
  });

  it("a pin holds the pane", () => {
    setFollow(true);
    setPinned(true);
    followCode({ project: "p", path: "a.ts" }, 10_000);
    expect(useCodeViewer.getState().current).toBeNull();
  });

  it("is desktop-only", () => {
    setMobile(true);
    setFollow(true);
    followCode({ project: "p", path: "a.ts" }, 10_000);
    expect(useCodeViewer.getState().current).toBeNull();
  });

  it("never re-routes docks", () => {
    setFollow(true);
    followCode({ project: "p", path: "a.ts" }, 10_000);
    expect(useUI.getState().rightCollapsed).toBe(true);
  });
});

// A coding delegate's edit (`fs.changed` from the bus): follow moves to that file's DIFF.
describe("followDiff", () => {
  it("does nothing while follow is OFF, pinned, or on a phone", () => {
    followDiff("app", "src/a.ts", 10_000);
    setFollow(true);
    setPinned(true);
    followDiff("app", "src/a.ts", 20_000);
    setPinned(false);
    setMobile(true);
    followDiff("app", "src/a.ts", 30_000);
    expect(useCodeViewer.getState().diffFocus).toBeNull();
    expect(useCodeViewer.getState().tab).toBe("file");
  });

  it("switches to the Diff tab on that project with the file picked; never re-routes docks", () => {
    setFollow(true);
    followDiff("app", "./src//a.ts", 10_000);
    const s = useCodeViewer.getState();
    expect(s.tab).toBe("diff");
    expect(s.diffProject).toBe("app");
    expect(s.diffFocus).toEqual({ project: "app", path: "src/a.ts", seq: 1 });
    expect(useUI.getState().rightCollapsed).toBe(true);
  });

  it("shares followCode's throttle: a burst lands on the LAST edit", () => {
    vi.useFakeTimers();
    setFollow(true);
    const t0 = Date.now();
    followCode({ project: "app", path: "read.ts" }, t0);
    followDiff("app", "b.ts", t0 + 100);
    followDiff("app", "c.ts", t0 + 200);
    expect(useCodeViewer.getState().diffFocus).toBeNull();
    vi.advanceTimersByTime(FOLLOW_THROTTLE_MS);
    expect(useCodeViewer.getState().diffFocus?.path).toBe("c.ts");
  });
});

// "Continue in Zed" scopes a hand-off to the pane's file only when it came from the chat being
// handed off, so every open records its originating chat.
describe("origin session", () => {
  it("an operator open is stamped with the ACTIVE chat; an explicit origin wins", async () => {
    const { chatStore } = await import("../chat/chat-store");
    chatStore.createSession();
    const active = chatStore.getSnapshot().currentSessionId;
    expect(active).toMatch(/^chat-/);
    openCode({ project: "p", path: "a.ts", source: "link" });
    expect(useCodeViewer.getState().current?.sessionId).toBe(active);
    openCode({ project: "p", path: "b.ts", source: "component", sessionId: "chat-bg" });
    expect(useCodeViewer.getState().current?.sessionId).toBe("chat-bg");
  });
});
