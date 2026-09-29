// MobileShell header actions (protoContent#551, action-button rule, card 1). The four header
// ACTIONS — Back, Menu, Search commands, New chat — must render via the DS `Button`
// (variant ghost, size md, icon), while the session-title switcher stays a sanctioned raw
// composite <button> and the centring spacer stays a plain <span>. createRoot/act + the real
// chat-store, like the other console UI suites (FleetRoom.test.tsx) — no testing-library dep.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { chatStore, unusedSession } from "../chat/chat-store";
// Raw stylesheet text — same source-guard pattern as mobileBottomInset.test.ts. `vitest.config.ts`
// opts src's CSS into processing so `?raw` returns the real text.
import mobileShellCss from "./mobile-shell.css?raw";
import { MobileShell } from "./MobileShell";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

type ShellProps = Parameters<typeof MobileShell>[0];

function baseProps(over: Partial<ShellProps> = {}): ShellProps {
  return {
    root: h("div", { "data-testid": "chat-root" }),
    pushed: null,
    title: "A pushed surface",
    showBack: false,
    onBack: () => {},
    onOpenDrawer: () => {},
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
