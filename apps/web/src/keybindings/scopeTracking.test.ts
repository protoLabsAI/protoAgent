// #3677 — a `scope: "chat"` binding must fire whenever the chat panel is the active region,
// not only while the composer textarea holds focus. A keydown's target is
// `document.activeElement`; clicking the transcript, a message, or (in WebKit) a `<button>`
// leaves focus on `<body>`, so `focusedScopes(e.target)` used to see nothing and drop every
// chat-scoped binding. The host now tracks the last pointerdown/focusin target and resolves
// the scope against it when the keydown target has fallen back to body/documentElement.
//
// These pin the pure `activeScopes` helper (the whole scope decision, unit-testable without
// React) and the real listener path (mount the hook, dispatch pointerdown then keydown,
// assert a scoped binding runs) — matching the createRoot/act pattern the sibling UI tests use.
import { act, createElement as h, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { registerKeybinding, type Keybinding } from "../ext/keybindingRegistry";
import { activeScopes, focusedScopes, useGlobalKeybindings } from "./useKeybindings";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Build the fixture: a chat scope holding a non-focusable message div and a <button>, plus an
// unscoped <nav> sibling. Mirrors the ChatSurface `<section data-kb-scope="chat">` root.
let scope: HTMLElement;
let msg: HTMLElement;
let button: HTMLButtonElement;
let textarea: HTMLTextAreaElement;
let navLink: HTMLElement;

beforeEach(() => {
  document.body.innerHTML = "";
  scope = document.createElement("section");
  scope.setAttribute("data-kb-scope", "chat");
  msg = document.createElement("div");
  msg.className = "msg";
  msg.textContent = "text";
  button = document.createElement("button");
  button.textContent = "tab";
  textarea = document.createElement("textarea");
  scope.append(msg, button, textarea);

  const nav = document.createElement("nav");
  navLink = document.createElement("a");
  navLink.textContent = "settings";
  nav.append(navLink);

  document.body.append(scope, nav);
});

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("focusedScopes — walks data-kb-scope up from the target", () => {
  it("collects the scope for an element inside a scoped root", () => {
    expect([...focusedScopes(msg)]).toEqual(["chat"]);
    expect([...focusedScopes(button)]).toEqual(["chat"]);
  });

  it("is empty for an unscoped element and for non-Element targets", () => {
    expect(focusedScopes(navLink).size).toBe(0);
    expect(focusedScopes(window).size).toBe(0);
    expect(focusedScopes(null).size).toBe(0);
  });
});

describe("activeScopes — the scope a keydown resolves against (#3677)", () => {
  it("r5: derives from a real focused element, ignoring the recorded one", () => {
    // e.target is the composer textarea (a real focused control): its chain wins, even when
    // the last-interacted record points at an unscoped element.
    expect(activeScopes(textarea, navLink).has("chat")).toBe(true);
    // …and a focused element OUTSIDE the scope yields no chat scope regardless of the record.
    expect(activeScopes(navLink, msg).has("chat")).toBe(false);
  });

  it("r1: falls back to the recorded element when target is <body>", () => {
    expect(activeScopes(document.body, msg).has("chat")).toBe(true);
  });

  it("r1/r2: fallback works for a non-focusable node and a <button>, and for window/null targets", () => {
    expect(activeScopes(window, msg).has("chat")).toBe(true);
    expect(activeScopes(null, button).has("chat")).toBe(true);
    expect(activeScopes(document.documentElement, button).has("chat")).toBe(true);
  });

  it("r3: an unscoped recorded element yields no scope", () => {
    expect(activeScopes(document.body, navLink).has("chat")).toBe(false);
    expect(activeScopes(document.body, navLink).size).toBe(0);
  });

  it("r4: no fallback once the recorded element's root is aria-hidden", () => {
    scope.setAttribute("aria-hidden", "true");
    expect(activeScopes(document.body, msg).has("chat")).toBe(false);
  });

  it("r4: no fallback once the recorded element's root is hidden", () => {
    scope.setAttribute("hidden", "");
    expect(activeScopes(document.body, msg).has("chat")).toBe(false);
  });

  it("r4: no fallback once the recorded element is detached from the DOM", () => {
    msg.remove();
    expect(activeScopes(document.body, msg).has("chat")).toBe(false);
  });

  it("no recorded element and no focused element resolves to nothing", () => {
    expect(activeScopes(document.body, null).size).toBe(0);
  });
});

// ── Real listener path ─────────────────────────────────────────────────────────────────
// Mount the hook in a minimal component (createRoot/act, like the sibling UI tests) so the
// actual capture-phase pointerdown/focusin listeners run, then dispatch on the real DOM.

function Host() {
  useGlobalKeybindings();
  return null;
}

let container: HTMLElement;
let root: Root;

function mountHost() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(h(Host)));
}

function unmountHost() {
  act(() => root.unmount());
  container.remove();
}

// A keydown whose target is <body> (activeElement === body): bubbles to the window host.
function bodyKeydown(key: string) {
  document.body.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
}

describe("useGlobalKeybindings — last-interaction fallback fires scoped bindings (#3677)", () => {
  it("r1/r2: pointerdown inside the chat scope then a body keydown runs the chat-scoped binding", () => {
    const runMsg = vi.fn();
    const runBtn = vi.fn();
    const offMsg = registerScoped("test.scope.msg", "f9", runMsg);
    mountHost();

    // Non-focusable message div (leaves focus on body).
    msg.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    bodyKeydown("F9");
    expect(runMsg).toHaveBeenCalledTimes(1);
    offMsg();

    // WebKit case: a <button> click doesn't focus it, so focus stays on body.
    const offBtn = registerScoped("test.scope.btn", "f8", runBtn);
    button.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    bodyKeydown("F8");
    expect(runBtn).toHaveBeenCalledTimes(1);
    offBtn();
    unmountHost();
  });

  it("focus falling back to <body> after a pointerdown in the scope doesn't erase the record", () => {
    const run = vi.fn();
    const off = registerScoped("test.scope.msg", "f9", run);
    mountHost();

    // Typing in the composer, then clicking a non-focusable message: the browser fires
    // pointerdown on the message, then focus falls back to <body> (focusin on body). The
    // body focusin must not overwrite the in-scope record.
    msg.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    document.body.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    bodyKeydown("F9");
    expect(run).toHaveBeenCalledTimes(1);

    off();
    unmountHost();
  });

  it("r5: a real focused target keeps its own scope AND the isEditableTarget typing gate", () => {
    const run = vi.fn();
    const off = registerScoped("test.scope.msg", "f9", run);
    mountHost();

    // Point the record at an UNSCOPED element, then fire the keydown FROM the textarea: the
    // real focused target's chain (chat) wins over the record, so scope still resolves…
    navLink.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    textarea.dispatchEvent(new KeyboardEvent("keydown", { key: "F9", bubbles: true, cancelable: true }));
    // …but the binding didn't opt into allowInInput and focus is in a textarea, so the typing
    // gate (editable = isEditableTarget(e.target)) suppresses it.
    expect(run).not.toHaveBeenCalled();

    off();
    unmountHost();
  });

  it("r3: a later pointerdown on an unscoped element stops the scoped binding from firing", () => {
    const run = vi.fn();
    const off = registerScoped("test.scope.msg", "f9", run);
    mountHost();

    msg.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    navLink.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    bodyKeydown("F9");
    expect(run).not.toHaveBeenCalled();

    off();
    unmountHost();
  });

  it("r7: the pointerdown and focusin capture listeners are added and removed with the hook", () => {
    const addSpy = vi.spyOn(window, "addEventListener");
    const removeSpy = vi.spyOn(window, "removeEventListener");
    mountHost();
    expect(addSpy).toHaveBeenCalledWith("pointerdown", expect.any(Function), { capture: true });
    expect(addSpy).toHaveBeenCalledWith("focusin", expect.any(Function), { capture: true });

    unmountHost();
    expect(removeSpy).toHaveBeenCalledWith("pointerdown", expect.any(Function), { capture: true });
    expect(removeSpy).toHaveBeenCalledWith("focusin", expect.any(Function), { capture: true });

    // Listener gone: a fresh interaction is no longer recorded, so the fallback can't fire.
    const run = vi.fn();
    const off = registerScoped("test.scope.msg", "f9", run);
    msg.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    bodyKeydown("F9");
    expect(run).not.toHaveBeenCalled();
    off();
  });
});

// Register a temporary chat-scoped binding for the listener-path tests, returning its
// unregister fn. Uses a bare-key combo (no editable gate hit, since the fallback target is body).
function registerScoped(id: string, defaultKeys: string, run: () => void): () => void {
  const binding: Keybinding = { id, label: id, defaultKeys, scope: "chat", run };
  return registerKeybinding(binding);
}
