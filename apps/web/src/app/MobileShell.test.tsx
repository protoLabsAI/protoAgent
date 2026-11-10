// MobileShell header actions (protoContent#551, action-button rule, card 1). The four header
// ACTIONS — Back, Menu, Search commands, New chat — must render via the DS `Button`
// (variant ghost, size md, icon), while the session-title switcher stays a sanctioned raw
// composite <button> and the centring spacer stays a plain <span>. createRoot/act + the real
// chat-store, like the other console UI suites (FleetRoom.test.tsx) — no testing-library dep.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chatStore, unusedSession } from "../chat/chat-store";
// Raw stylesheet text — same source-guard pattern as mobileBottomInset.test.ts. `vitest.config.ts`
// opts src's CSS into processing so `?raw` returns the real text.
import mobileShellCss from "./mobile-shell.css?raw";
import appSource from "./App.tsx?raw";
import { useUI } from "../state/uiStore";
import { needsYouCount } from "./App";
import type { WorkItem, WorkItems, WorkSource } from "../work/useWorkItems";
import { MobileShell, MobileWorkHandle } from "./MobileShell";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

type ShellProps = Parameters<typeof MobileShell>[0];

function baseProps(over: Partial<ShellProps> = {}): ShellProps {
  return {
    root: h("div", { "data-testid": "chat-root" }, h(MobileWorkHandle)),
    pushed: null,
    title: "A pushed surface",
    showBack: false,
    onBack: () => {},
    onOpenDrawer: () => {},
    needsYouCount: 0,
    onOpenWork: () => {},
    sessionSheetOpen: false,
    onSessionSheetChange: () => {},
    ...over,
  };
}

function mount(over: Partial<ShellProps> = {}) {
  act(() => {
    root.render(h(MobileShell, baseProps(over)));
  });
}

const byLabel = (label: string) =>
  container.querySelector<HTMLElement>(`[aria-label="${label}"]`);

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

// A DS `Button variant="ghost" size="md" icon` renders `<button class="pl-btn pl-btn--ghost
// pl-btn--icon">` (size md is the default, so it adds no size class). Asserting the class proves
// the swap away from the raw `mshell-head-btn` <button>, which is what the #551 audit flags.
function expectDsGhostIcon(el: HTMLElement | null, label: string) {
  expect(el, `expected an element labelled "${label}"`).not.toBeNull();
  expect(el!.tagName).toBe("BUTTON");
  expect(el!.classList.contains("pl-btn")).toBe(true);
  expect(el!.classList.contains("pl-btn--ghost")).toBe(true);
  expect(el!.classList.contains("pl-btn--icon")).toBe(true);
  // No button carries the retired hand-rolled class any more (acceptance r4).
  expect(el!.classList.contains("mshell-head-btn")).toBe(false);
}

describe("MobileShell header actions render via DS Button (#551)", () => {
  it("renders Menu, Search commands and New chat as ghost icon Buttons at the chat root", () => {
    mount({ showBack: false });

    const menu = byLabel("Menu");
    expectDsGhostIcon(menu, "Menu");
    // The hook a swarm of drawer e2e specs pin — must survive the swap unchanged.
    expect(menu!.getAttribute("data-testid")).toBe("header-menu");
    expect(menu!.getAttribute("type")).toBe("button");

    const search = byLabel("Search commands");
    expectDsGhostIcon(search, "Search commands");
    expect(search!.getAttribute("title")).toBe("Search commands, surfaces and agents");

    const newChat = byLabel("New chat");
    expectDsGhostIcon(newChat, "New chat");
    // `disabled` flows through the DS Button exactly as it did on the raw button: the "+" is a
    // no-op when the blank it would reuse is already the current session. Compute the expected
    // state from the live store the same way the component does.
    const snap = chatStore.getSnapshot();
    const blank = unusedSession(snap);
    const noop = blank != null && blank.id === snap.currentSessionId;
    expect((newChat as HTMLButtonElement).disabled).toBe(noop);
    expect(newChat!.getAttribute("title")).toBe(noop ? "This chat is already empty" : "New chat");
  });

  it("keeps the session-title switcher a raw composite <button>, not a DS Button", () => {
    mount({ showBack: false });

    const title = container.querySelector<HTMLElement>("button.mshell-title");
    expect(title, "expected the .mshell-title switcher button").not.toBeNull();
    // Sanctioned composite trigger — it must NOT have been swapped for a DS Button.
    expect(title!.classList.contains("pl-btn")).toBe(false);
    expect(title!.getAttribute("aria-haspopup")).toBe("dialog");
  });

  it("renders Back as a ghost icon Button and keeps the spacer a plain span", () => {
    mount({ showBack: true });

    expectDsGhostIcon(byLabel("Back"), "Back");

    // The empty centring spacer stays a <span aria-hidden> with the header-button footprint —
    // it must not become a button (nothing to activate) and must keep its width.
    const spacer = container.querySelector<HTMLElement>("span.mshell-head-spacer");
    expect(spacer, "expected the .mshell-head-spacer span").not.toBeNull();
    expect(spacer!.tagName).toBe("SPAN");
    expect(spacer!.getAttribute("aria-hidden")).not.toBeNull();

    // The Back branch shows a static title, not the switcher.
    expect(container.querySelector("button.mshell-title")).toBeNull();
  });
});

describe("mobile-shell.css after the DS Button swap (#551)", () => {
  it("drops the hand-rolled .mshell-head-btn rule and its :active state", () => {
    expect(mobileShellCss).not.toMatch(/\.mshell-head-btn\b/);
  });

  it("keeps the centring spacer sized to the 44px touch floor", () => {
    const rule = /\.mshell-head-spacer\s*\{[^}]*\}/.exec(mobileShellCss);
    expect(rule, "expected a `.mshell-head-spacer` rule in mobile-shell.css").not.toBeNull();
    expect(rule![0]).toMatch(/min-width:\s*44px/);
  });

  it("adds no new literal colour or px sizes to the spacer (tokens + the 44px floor only)", () => {
    const rule = /\.mshell-head-spacer\s*\{[^}]*\}/.exec(mobileShellCss)![0];
    // No hex/rgb literals — colour comes from the DS Button now, the spacer has none.
    expect(rule).not.toMatch(/#[0-9a-fA-F]{3,}|rgba?\(/);
    // The only literal px allowed is the shared 44px HIG touch floor (min-width/min-height).
    for (const px of rule.match(/\d+px/g) ?? []) expect(px).toBe("44px");
  });
});


describe("phone Work entry points", () => {
  function pointer(target: HTMLElement, type: string, x: number, y: number) {
    act(() => target.dispatchEvent(new PointerEvent(type, {
      bubbles: true, pointerId: 1, isPrimary: true, button: 0, clientX: x, clientY: y,
    })));
  }

  function swipe(dx: number, dy: number) {
    const handle = byLabel("Open Work")!;
    handle.setPointerCapture = vi.fn();
    pointer(handle, "pointerdown", 100, 200);
    pointer(handle, "pointerup", 100 + dx, 200 + dy);
    act(() => handle.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 })));
  }

  it.each([[0, -60, 1], [0, -48, 1], [0, -30, 0], [60, -60, 0], [0, 60, 0]])(
    "handles travel (%i, %i) without a duplicate click",
    (dx, dy, calls) => {
      const onOpenWork = vi.fn();
      mount({ onOpenWork });
      swipe(dx, dy);
      expect(onOpenWork).toHaveBeenCalledTimes(calls);
    },
  );

  it("opens by tap and keyboard-generated click on a DS ghost button", () => {
    const onOpenWork = vi.fn();
    mount({ onOpenWork });
    const handle = byLabel("Open Work")!;
    expect(handle.classList.contains("pl-btn--ghost")).toBe(true);
    swipe(0, 0);
    act(() => handle.click());
    expect(onOpenWork).toHaveBeenCalledTimes(2);
  });

  it.each([[1, 0], [0, -1], [-1, 1], [4, -4], [0, 8]])(
    "opens once for a tap with finger jitter (%i, %i)",
    (dx, dy) => {
      const onOpenWork = vi.fn();
      mount({ onOpenWork });
      swipe(dx, dy);
      expect(onOpenWork).toHaveBeenCalledTimes(1);
    },
  );

  it("allows a jittery tap and keyboard activation after a rejected drag", () => {
    const onOpenWork = vi.fn();
    mount({ onOpenWork });
    swipe(0, -30);
    expect(onOpenWork).not.toHaveBeenCalled();
    act(() => byLabel("Open Work")!.click());
    expect(onOpenWork).toHaveBeenCalledTimes(1);
    swipe(1, -1);
    expect(onOpenWork).toHaveBeenCalledTimes(2);
  });

  it("does not listen on the message root", () => {
    const onOpenWork = vi.fn();
    mount({ onOpenWork });
    const chat = container.querySelector<HTMLElement>('[data-testid="chat-root"]')!;
    pointer(chat, "pointerdown", 100, 200);
    pointer(chat, "pointerup", 100, 100);
    expect(onOpenWork).not.toHaveBeenCalled();
  });

  it.each([{ sheetOpen: true }, { sessionSheetOpen: true }])("ignores gestures under an open sheet: %o", (props) => {
    const onOpenWork = vi.fn();
    mount({ ...props, onOpenWork });
    swipe(0, -60);
    expect(onOpenWork).not.toHaveBeenCalled();
  });

  it("ignores canceled pointers", () => {
    const onOpenWork = vi.fn();
    mount({ onOpenWork });
    const handle = byLabel("Open Work")!;
    handle.setPointerCapture = vi.fn();
    pointer(handle, "pointerdown", 100, 200);
    pointer(handle, "pointercancel", 100, 180);
    pointer(handle, "pointerup", 100, 100);
    expect(onOpenWork).not.toHaveBeenCalled();
  });

  it("omits the handle on pushed and background views and outside the phone shell", () => {
    mount({ showBack: true, pushed: h("div") });
    expect(byLabel("Open Work")).toBeNull();
    mount({ showBack: true });
    expect(byLabel("Open Work")).toBeNull();
    act(() => root.render(h(MobileWorkHandle)));
    expect(byLabel("Open Work")).toBeNull();
    expect(mobileShellCss).toMatch(/@media \(min-width: 768px\)\s*\{\s*\.mshell-work-handle \{ display: none;/);
  });

  // Exercise App's actual drawer callback with the real surface store.
  it("opens Work in place on the phone while other items and desktop retain their navigation", () => {
    const body = /onSelectSurface=\{\(id\) => \{([\s\S]*?)\n      \}\}/.exec(appSource)![1];
    const setMobileActive = vi.fn();
    const select = new Function("isMobile", "useUI", "setMobileActive", "id", body);
    const saved = useUI.getState();
    try {
      useUI.setState({ mobileActive: "chat", surfaceModal: null, surfaceStates: { work: { section: "goals" } } });
      select(true, useUI, setMobileActive, "work");
      expect(setMobileActive).not.toHaveBeenCalled();
      expect(useUI.getState().surfaceModal).toEqual({ viewId: "work", state: { section: "goals" } });
      expect(useUI.getState().mobileActive).toBe("chat");
      select(true, useUI, setMobileActive, "knowledge");
      expect(setMobileActive).toHaveBeenLastCalledWith("knowledge");
      select(false, useUI, setMobileActive, "work");
      expect(setMobileActive).toHaveBeenLastCalledWith("work");
    } finally {
      useUI.setState(saved);
    }
  });

  it("routes phone decision notifications to Work after closing Sessions", () => {
    const body = /useLayoutEffect\(\(\) => \{\n(    if \(decisions.panelOpen \|\| surfaceModal\)[\s\S]*?)\n  \},/.exec(appSource)![1];
    const calls: unknown[] = [];
    new Function("decisions", "surfaceModal", "isMobile", "setSessionSheetRequested", "useUI", body)(
      { panelOpen: true, closePanel: () => calls.push("close decisions") }, null, true,
      (open: boolean) => calls.push(["sessions", open]),
      { getState: () => ({ openSurfaceModal: (...args: unknown[]) => calls.push(args) }) },
    );
    expect(calls).toEqual([["sessions", false], "close decisions", ["work", { section: "decisions" }]]);
  });
});

describe("phone Needs you indicator", () => {
  it.each([[0, "Menu"], [1, "Menu, 1 needs you"], [3, "Menu, 3 need you"]] as const)(
    "shows the count %i in the accessible name",
    (count, label) => {
      mount({ needsYouCount: count });
      const menu = byLabel(label)!;
      expectDsGhostIcon(menu, label);
      expect(menu.querySelectorAll(".pl-dot")).toHaveLength(count > 0 ? 1 : 0);
      if (count > 0) {
        expect(menu.querySelector(".pl-dot--warning")).not.toBeNull();
        expect(menu.querySelector(".pl-dot--pulse")).toBeNull();
      }
    },
  );

  it("removes the dot when the last item no longer needs you", () => {
    mount({ needsYouCount: 1 });
    mount({ needsYouCount: 0 });
    expect(byLabel("Menu")!.querySelector(".pl-dot")).toBeNull();
  });

  it("counts the lane across chats and sources, ignoring error and loading data", () => {
    const item = (status: WorkItem["status"], sessionId: string): WorkItem => ({
      id: sessionId, kind: "decision", title: "Question", meta: "", raw: {}, status, sessionId,
    });
    const source = (status: WorkSource["status"], items: WorkItem[] = []): WorkSource => ({
      status, items, loadedAt: null, error: null, retry: () => {},
    });
    const sources: WorkItems = {
      decisions: source("ready", [item("needs-you", "current"), item("needs-you", "other")]),
      goals: source("stale", [item("needs-you", "third")]),
      watches: source("error", [item("needs-you", "error")]),
      tasks: source("loading", [item("needs-you", "loading")]),
      schedules: source("ready", [item("scheduled", "scheduled")]),
      background: source("ready", [item("running", "running")]),
      activity: source("ready", [item("failed", "failed"), item("done", "done")]),
      directives: source("ready", [item("waiting", "waiting")]),
    };
    expect(needsYouCount(sources)).toBe(3);
    sources.decisions.items = [];
    expect(needsYouCount(sources)).toBe(1);
    sources.goals.items = [];
    expect(needsYouCount(sources)).toBe(0);
  });
});
