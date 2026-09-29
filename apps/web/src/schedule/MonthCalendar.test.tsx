// #551 (action-button rule, card 4a): the Previous/Next month controls are DS Buttons
// (ghost, sm, icon) — not the hand-rolled `.cal-nav`. The `.cal-day` grid cells stay raw
// (they are grid cells, not actions). createRoot/act + querySelector, like the other console
// UI suites. `selected` seeds the month in view so the title is deterministic without a clock.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { MonthCalendar } from "./MonthCalendar";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

function mount(props: { selected: string; onSelect?: (iso: string) => void; today?: string }) {
  act(() => root.render(h(MonthCalendar, { onSelect: () => {}, ...props })));
}

const nav = (label: string) => container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
const title = () => container.querySelector(".cal-title")?.textContent;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("MonthCalendar nav controls (#551 action-button rule)", () => {
  it("Previous/Next month are DS Buttons: ghost + sm + icon, type=button, aria-labels intact", () => {
    mount({ selected: "2026-03-15" });
    for (const label of ["Previous month", "Next month"]) {
      const btn = nav(label);
      expect(btn, `${label} control`).not.toBeNull();
      expect(btn!.className).toContain("pl-btn");
      expect(btn!.className).toContain("pl-btn--ghost");
      expect(btn!.className).toContain("pl-btn--sm");
      expect(btn!.className).toContain("pl-btn--icon");
      expect(btn!.getAttribute("type")).toBe("button");
      // The chevron glyph rides along unchanged.
      expect(btn!.querySelector("svg")).not.toBeNull();
      // The hand-rolled class is gone.
      expect(btn!.classList.contains("cal-nav")).toBe(false);
    }
  });

  it("the step handlers still move the month in view", () => {
    mount({ selected: "2026-03-15" });
    expect(title()).toBe("March 2026");
    act(() => nav("Previous month")!.click());
    expect(title()).toBe("February 2026");
    act(() => nav("Next month")!.click());
    act(() => nav("Next month")!.click());
    expect(title()).toBe("April 2026");
  });

  it("the day cells stay raw grid buttons — not DS Buttons", () => {
    mount({ selected: "2026-03-15" });
    const days = container.querySelectorAll("button.cal-day");
    expect(days.length).toBeGreaterThan(0);
    for (const day of days) {
      expect(day.className).not.toContain("pl-btn");
    }
  });
});
