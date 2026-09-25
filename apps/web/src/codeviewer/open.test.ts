import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useUI } from "../state/uiStore";
import { CODE_PANE_WIDTH, FOLLOW_THROTTLE_MS, followCode, openCode, placeCodeSurface, resetFollowThrottle } from "./open";
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
});

afterEach(() => {
  vi.useRealTimers();
});

describe("placeCodeSurface — never on chat's dock", () => {
  it("leaves it on the right when chat is on the left", () => {
    expect(placeCodeSurface()).toBe("right");
    expect(useUI.getState().railOrder.right).toContain("code");
  });
  it("moves it to the left when chat is on the right with it", () => {
    rail(["knowledge"], ["chat", "work", "code"]);
    expect(placeCodeSurface()).toBe("left");
    expect(useUI.getState().railOrder.left).toContain("code");
    expect(useUI.getState().railOrder.right).not.toContain("code");
  });
  it("keeps an operator's own non-chat dock (bottom)", () => {
    rail(["chat"], ["work"], ["code"]);
    expect(placeCodeSurface()).toBe("bottom");
  });
  it("puts a missing/hidden surface on the side away from chat", () => {
    useUI.setState({ railOrder: { left: ["chat"], right: ["work"], bottom: [], hidden: ["code"] } });
    expect(placeCodeSurface()).toBe("right");
    expect(useUI.getState().railOrder.hidden).not.toContain("code");
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
