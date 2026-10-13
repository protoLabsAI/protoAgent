// The utility-bar pill is now a DS `<Button icon size="xs" variant="ghost">` rather than a
// bare hand-rolled `<button>` (#3684). This suite guards the swap: the pill must stay
// a real button that keeps its testid, aria-label, title fallback, children, click/context-menu
// handlers and its optional Tooltip wrapper — the contract the utility bar and its e2e specs
// depend on. createRoot/act + the real DS components, like the other console UI suites
// (SetupGapBanner.test.tsx) — no testing-library dep.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { UtilityWidget } from "./UtilityWidget";

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

/** The pill, wherever it renders (a Tooltip clones the trigger inline, so it stays in the tree). */
const pill = () => document.querySelector<HTMLButtonElement>('[data-testid="util-widget-inbox"]');

describe("UtilityWidget — the DS-Button pill (#3684)", () => {
  it("renders the pill as a real DS Button carrying its testid, aria-label and children", () => {
    act(() =>
      root.render(
        h(UtilityWidget, {
          testId: "util-widget-inbox",
          label: "Inbox",
          dialogTitle: "Inbox",
          icon: h("span", { "data-testid": "pill-icon" }, "I"),
          badge: h("span", { "data-testid": "pill-badge" }, "3"),
          children: "body",
        }),
      ),
    );
    const btn = pill();
    expect(btn).not.toBeNull();
    expect(btn!.tagName).toBe("BUTTON");
    expect(btn!.getAttribute("aria-label")).toBe("Inbox");
    // The whole point of the card: the pill is a DS Button (carries the `pl-btn` class),
    // not a bare hand-rolled element.
    expect(btn!.classList.contains("pl-btn")).toBe(true);
    // The host still owns the glyph + badge children.
    expect(btn!.querySelector('[data-testid="pill-icon"]')).not.toBeNull();
    expect(btn!.querySelector('[data-testid="pill-badge"]')).not.toBeNull();
  });

  it("falls back to the label as the native title when there is no hover info", () => {
    act(() =>
      root.render(
        h(UtilityWidget, {
          testId: "util-widget-inbox",
          label: "Inbox",
          dialogTitle: "Inbox",
          icon: h("span", null, "i"),
          children: "body",
        }),
      ),
    );
    expect(pill()!.getAttribute("title")).toBe("Inbox");
  });

  it("drops the native title and wraps in a Tooltip when hover info is supplied (no doubled label)", () => {
    act(() =>
      root.render(
        h(UtilityWidget, {
          testId: "util-widget-inbox",
          label: "Inbox",
          info: "2 unread",
          dialogTitle: "Inbox",
          icon: h("span", null, "i"),
          children: "body",
        }),
      ),
    );
    const btn = pill();
    // Still the same testid'd button, still reachable — the Tooltip renders its trigger inline.
    expect(btn).not.toBeNull();
    // `title` is undefined so the Tooltip's popover isn't shadowed by a native title bubble.
    expect(btn!.getAttribute("title")).toBeNull();
  });

  it("opens the dialog on click and fires onOpen", () => {
    let opened = 0;
    act(() =>
      root.render(
        h(UtilityWidget, {
          testId: "util-widget-inbox",
          label: "Inbox",
          dialogTitle: "Inbox panel",
          onOpen: () => (opened += 1),
          icon: h("span", null, "i"),
          children: h("p", { "data-testid": "dialog-body" }, "panel contents"),
        }),
      ),
    );
    // The dialog body mounts only while open.
    expect(document.querySelector('[data-testid="dialog-body"]')).toBeNull();
    act(() => pill()!.click());
    expect(opened).toBe(1);
    expect(document.querySelector('[data-testid="dialog-body"]')).not.toBeNull();
  });

  // DS 0.63 card 3a (#3688): every content dialog opts into `padding="roomy"` so its body keeps
  // its 24px padding once the app-wide `.pl-dialog__body` theme.css rule is deleted. The shared
  // utility-pill dialog is the one every UtilityWidget consumer renders through.
  it("gives the opened utility-pill dialog roomy body padding (#3688)", () => {
    act(() =>
      root.render(
        h(UtilityWidget, {
          testId: "util-widget-inbox",
          label: "Inbox",
          dialogTitle: "Inbox panel",
          icon: h("span", null, "i"),
          children: h("p", { "data-testid": "dialog-body" }, "panel contents"),
        }),
      ),
    );
    act(() => pill()!.click());
    // The DS Dialog renders its body as `.pl-dialog__body`; `padding="roomy"` adds the `--roomy`
    // modifier alongside it (`none` would add `--flush`, the default adds neither).
    const body = document.querySelector(".pl-dialog__body");
    expect(body).not.toBeNull();
    expect(body!.classList.contains("pl-dialog__body--roomy")).toBe(true);
    // The panel content still lives inside that roomy body.
    expect(body!.querySelector('[data-testid="dialog-body"]')).not.toBeNull();
  });

  it("forwards a right-click to onContextMenu (ADR 0036 context-menu wiring)", () => {
    let menus = 0;
    act(() =>
      root.render(
        h(UtilityWidget, {
          testId: "util-widget-inbox",
          label: "Inbox",
          dialogTitle: "Inbox",
          onContextMenu: () => (menus += 1),
          icon: h("span", null, "i"),
          children: "body",
        }),
      ),
    );
    act(() => {
      pill()!.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    });
    expect(menus).toBe(1);
  });
});
