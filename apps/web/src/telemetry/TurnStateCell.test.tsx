// The Recent turns State cell shows why a failed or canceled turn ended (#4726). The
// server stores the reason in the turns row's `error` column; before this the console
// only had the state badge, so a failed turn could not say why without opening its trace.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { TurnStateCell } from "./TelemetrySurface";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const errorEl = () => container.querySelector('[data-testid="telemetry-turn-error"]');

describe("TurnStateCell", () => {
  it("shows a failed turn's stored error, with the full text on hover", () => {
    const reason = "upstream 429: rate limited by the provider, retry after 20s";
    act(() => root.render(h(TurnStateCell, { turn: { state: "failed", error: reason } })));
    expect(container.textContent).toContain("failed");
    expect(errorEl()?.textContent).toBe(reason);
    expect(errorEl()?.getAttribute("title")).toBe(reason);
  });

  it("expands to the full reason on a tap, since touch has no hover tooltip", () => {
    const reason = "upstream 500: " + "x".repeat(200);
    act(() => root.render(h(TurnStateCell, { turn: { state: "failed", error: reason } })));
    const el = errorEl() as HTMLButtonElement;
    expect(el.tagName).toBe("BUTTON");
    expect(el.getAttribute("aria-expanded")).toBe("false");
    act(() => el.click());
    expect(el.getAttribute("aria-expanded")).toBe("true");
    expect(el.hasAttribute("data-expanded")).toBe(true);
    act(() => el.click());
    expect(el.hasAttribute("data-expanded")).toBe(false);
  });

  it("shows a canceled turn's stop reason", () => {
    const reason = "stalled: no progress for 300s in tool web_fetch";
    act(() => root.render(h(TurnStateCell, { turn: { state: "canceled", error: reason } })));
    expect(errorEl()?.textContent).toBe(reason);
  });

  it("renders just the badge for a completed turn, an empty error, or a pre-column row", () => {
    act(() => root.render(h(TurnStateCell, { turn: { state: "completed", error: "stale text" } })));
    expect(errorEl()).toBeNull();
    act(() => root.render(h(TurnStateCell, { turn: { state: "failed", error: "  " } })));
    expect(errorEl()).toBeNull();
    act(() => root.render(h(TurnStateCell, { turn: { state: "failed", error: null } })));
    expect(errorEl()).toBeNull();
    expect(container.textContent).toBe("failed");
  });
});
