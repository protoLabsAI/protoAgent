import { act, createElement as h } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { resetDismissedGoals, useDismissedGoals } from "./dismissedGoals";

// A dismissal made in ANOTHER browser tab arrives as a `storage` event; this tab re-reads it
// so the finished goal hides everywhere, not just where it was dismissed.
describe("dismissed goals sync across browser tabs", () => {
  afterEach(() => resetDismissedGoals());

  it("picks up a dismissal written by another tab", () => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    let seen: ReadonlySet<string> = new Set();
    const Probe = () => {
      seen = useDismissedGoals();
      return null;
    };
    const root = createRoot(document.createElement("div"));
    act(() => root.render(h(Probe)));
    expect(seen.has("s1:100")).toBe(false);
    act(() => {
      window.localStorage.setItem("protoagent.goals.dismissed", JSON.stringify(["s1:100"]));
      window.dispatchEvent(new StorageEvent("storage", { key: "protoagent.goals.dismissed" }));
    });
    expect(seen.has("s1:100")).toBe(true);
    act(() => root.unmount());
  });
});
