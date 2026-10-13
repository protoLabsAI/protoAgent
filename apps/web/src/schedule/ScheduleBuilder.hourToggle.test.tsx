// The Repeat tab's 12h/24h toggle is a DS Button (ghost, xs) per the #551 action-button
// rule, not the old hand-rolled `.hour-toggle` <button>. Its title, click handler and label
// must survive the swap: clicking flips the label and the time input between the 24h
// <input type=time> and the 12h dropdown trio. createRoot/act + hyperscript, like
// ThemeSurface.test.tsx.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ScheduleBuilder } from "./ScheduleBuilder";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TOGGLE_TITLE = "Switch between 24-hour and 12-hour input";

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

function renderRepeatBuilder() {
  act(() => {
    root.render(
      h(ScheduleBuilder, {
        initial: { parsed: { mode: "repeat", freq: "daily", time: "09:00", dow: 1 }, timezone: "" },
        onChange: () => {},
      }),
    );
  });
}

const toggle = () => container.querySelector(`[title="${TOGGLE_TITLE}"]`) as HTMLButtonElement | null;

describe("ScheduleBuilder — 12h/24h toggle (#551 DS Button)", () => {
  it("renders the toggle as a native DS Button, not the old .hour-toggle element", () => {
    renderRepeatBuilder();
    const btn = toggle();
    expect(btn, "expected the 12h/24h toggle to render on the Repeat tab").not.toBeNull();
    // DS Button spreads ButtonHTMLAttributes onto a real <button>; type stays "button".
    expect(btn!.tagName).toBe("BUTTON");
    expect(btn!.getAttribute("type")).toBe("button");
    // The retired hand-rolled class must be gone from the markup.
    expect(btn!.classList.contains("hour-toggle")).toBe(false);
    expect(container.querySelector(".hour-toggle")).toBeNull();
  });

  it("keeps the label and handler: clicking flips 24h ⇆ 12h and swaps the time control", () => {
    renderRepeatBuilder();
    expect(toggle()!.textContent?.trim()).toBe("24h");
    expect(container.querySelector('[data-testid="schedule-time"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="schedule-time-12h"]')).toBeNull();

    act(() => toggle()!.click());
    expect(toggle()!.textContent?.trim()).toBe("12h");
    expect(container.querySelector('[data-testid="schedule-time-12h"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="schedule-time"]')).toBeNull();

    act(() => toggle()!.click());
    expect(toggle()!.textContent?.trim()).toBe("24h");
    expect(container.querySelector('[data-testid="schedule-time"]')).not.toBeNull();
  });
});
