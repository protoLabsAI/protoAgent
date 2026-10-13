// CodeChangeWatch (ADR 0112): the bus → code pane wire. An `fs.changed` burst re-fetches the
// pane's diff ONCE (debounced, coalesced), and with Follow on a DELEGATE's edit moves the
// pane to that file on the Diff tab — the agent's own edits follow through the live tool
// stream instead, so they must not jump here too.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Listener = (data: Record<string, unknown>, topic: string) => void;
const bus = vi.hoisted(() => ({ subs: new Map<string, Set<Listener>>() }));

vi.mock("../lib/events", () => ({
  onTopic: (pattern: string, fn: Listener) => {
    const set = bus.subs.get(pattern) ?? new Set<Listener>();
    set.add(fn);
    bus.subs.set(pattern, set);
    return () => set.delete(fn);
  },
}));

import { CodeChangeWatch } from "./CodeChangeWatch";
import { setCodePaneEnabled } from "./enabled";
import { diffQueryKey, REFRESH_DEBOUNCE_MS } from "./liveRefresh";
import { resetFollowThrottle } from "./open";
import { resetCodeViewer, setFollow, setPinned, useCodeViewer } from "./store";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;
let qc: QueryClient;

function publish(data: Record<string, unknown>) {
  for (const fn of bus.subs.get("fs.changed") ?? []) fn(data, "fs.changed");
}

function mount() {
  qc = new QueryClient();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(h(QueryClientProvider, { client: qc }, h(CodeChangeWatch))));
}

beforeEach(() => {
  vi.useFakeTimers();
  bus.subs.clear();
  resetCodeViewer();
  resetFollowThrottle();
  window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as never;
  setCodePaneEnabled(true);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  setCodePaneEnabled(false);
  vi.useRealTimers();
});

describe("CodeChangeWatch", () => {
  it("a burst of fs.changed re-fetches the project's diff once, after the debounce", () => {
    mount();
    const spy = vi.spyOn(qc, "invalidateQueries");
    publish({ project: "app", paths: ["src/a.ts"], source: "delegate" });
    publish({ project: "app", paths: ["src/b.ts"], source: "delegate" });
    publish({ project: "app", paths: ["src/a.ts"], source: "agent" });
    expect(spy).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(REFRESH_DEBOUNCE_MS));
    const diffCalls = spy.mock.calls.filter(([f]) => JSON.stringify(f?.queryKey) === JSON.stringify(diffQueryKey("app")));
    expect(diffCalls).toHaveLength(1);
    const fileKeys = spy.mock.calls.map(([f]) => f?.queryKey).filter((k) => k?.[0] === "code-pane-file");
    expect(fileKeys).toEqual([
      ["code-pane-file", "app", "src/a.ts"],
      ["code-pane-file", "app", "src/b.ts"],
    ]);
  });

  it("does not subscribe while the code pane toolset is off", () => {
    setCodePaneEnabled(false);
    mount();
    expect(bus.subs.get("fs.changed")?.size ?? 0).toBe(0);
  });

  it("Follow on: a delegate's edit switches the pane to that file on the Diff tab", () => {
    mount();
    act(() => setFollow(true));
    act(() => publish({ project: "app", paths: ["src/calc.py"], source: "delegate", target: "claude-code" }));
    const s = useCodeViewer.getState();
    expect(s.tab).toBe("diff");
    expect(s.diffProject).toBe("app");
    expect(s.diffFocus).toMatchObject({ project: "app", path: "src/calc.py" });
  });

  it("Follow on: the agent's OWN edit does not jump here (the tool stream follows it)", () => {
    mount();
    act(() => setFollow(true));
    act(() => publish({ project: "app", paths: ["src/calc.py"], source: "agent" }));
    expect(useCodeViewer.getState().diffFocus).toBeNull();
    expect(useCodeViewer.getState().tab).toBe("file");
  });

  it("Follow off, or pinned: a delegate's edit refreshes but never moves the pane", () => {
    mount();
    act(() => publish({ project: "app", paths: ["a.py"], source: "delegate" }));
    expect(useCodeViewer.getState().diffFocus).toBeNull();
    act(() => {
      setFollow(true);
      setPinned(true);
    });
    act(() => publish({ project: "app", paths: ["a.py"], source: "delegate" }));
    expect(useCodeViewer.getState().diffFocus).toBeNull();
  });
});
