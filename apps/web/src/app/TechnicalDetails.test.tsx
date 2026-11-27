// The shared compact Technical details disclosure: a real button carrying aria-expanded,
// closed by default, its body mounted only while open, and its state per instance.

import { createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { TechnicalDetails } from "./TechnicalDetails";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLDivElement;
beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

const toggles = () => [...host.querySelectorAll<HTMLButtonElement>(".technical-details__toggle")];

describe("TechnicalDetails", () => {
  it("is a closed native button toggle with a hidden chevron, then opens and closes again", async () => {
    await act(async () => root.render(h(TechnicalDetails, { children: h("p", null, "task-123") })));
    const [toggle] = toggles();
    expect(toggle.tagName).toBe("BUTTON");
    expect(toggle.type).toBe("button"); // keyboard: Enter and Space activate a native button
    expect(toggle.textContent).toBe("Technical details");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.hasAttribute("aria-controls")).toBe(false);
    expect(toggle.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    expect(host.textContent).not.toContain("task-123");
    // No DS Accordion chrome: the compact treatment is not a bordered card.
    expect(host.querySelector(".pl-accordion, details")).toBeNull();

    await act(async () => toggle.click());
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const body = host.querySelector(".technical-details__body")!;
    expect(toggle.getAttribute("aria-controls")).toBe(body.id);
    expect(body.textContent).toBe("task-123");

    await act(async () => toggle.click());
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(host.textContent).not.toContain("task-123");
  });

  it("keeps each instance's open state its own and passes a className through", async () => {
    await act(async () => root.render(h("div", null,
      h(TechnicalDetails, { className: "storage-details", children: "first" }),
      h(TechnicalDetails, { children: "second" }))));
    const [a, b] = toggles();
    expect(host.querySelector(".technical-details.storage-details")).not.toBeNull();
    await act(async () => b.click());
    expect(a.getAttribute("aria-expanded")).toBe("false");
    expect(b.getAttribute("aria-expanded")).toBe("true");
    expect(host.textContent).toContain("second");
    expect(host.textContent).not.toContain("first");
  });
});
