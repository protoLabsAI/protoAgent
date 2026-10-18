// AuthGate is a BLOCKING modal (#1921): while a 401 stands, the app behind it is
// dead, so the gate must not be dismissible — no backdrop click, no Escape, no `×`,
// no "Not now". The blocking behavior rides the DS Dialog's contract (omit `onClose`),
// so this file pins both halves: (1) the DS contract itself — a Dialog WITH `onClose`
// stays dismissible (normal settings dialogs must still close), a Dialog WITHOUT it
// does not; and (2) AuthGate wires itself as the non-dismissible variant.
//
// jsdom + react-dom/client (the console has no @testing-library; the unit harness is
// `.test.ts` only, so we build elements with React.createElement rather than JSX).
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Dialog } from "@protolabsai/ui/overlays";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthGate } from "./AuthGate";
import { authRequired, clearAuthRequired, notifyAuthRequired } from "../lib/auth";

// Tell React we're inside an act-capable environment so effect flushing is clean.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

function mount(node: Parameters<Root["render"]>[0]) {
  act(() => {
    root.render(node);
  });
}

// @protolabsai/ui ≥ 0.56 portals Dialog/Drawer/Lightbox to <body> (`overlayPortal`) so a
// parent surface's scoped `pl-*` rules can't reach them. Overlay content is therefore a
// SIBLING of `container`, never inside it — query the portal target. (`container` still
// owns unmount cleanup; React removes the portal with the tree.)
function inPortal<E extends Element = Element>(sel: string) {
  return document.body.querySelector<E>(sel);
}

function pressEscape() {
  act(() => {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
}

function clickBackdrop() {
  const overlay = inPortal<HTMLElement>(".pl-overlay");
  if (!overlay) throw new Error("no .pl-overlay rendered");
  act(() => {
    overlay.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  });
}

function buttonByText(text: string) {
  return [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === text);
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("DS Dialog dismissal contract", () => {
  it("a Dialog WITH onClose stays dismissible (× button, Escape + backdrop close)", () => {
    const onClose = vi.fn();
    mount(h(Dialog, { open: true, title: "Settings", onClose }, "body"));

    // The × close affordance renders only when a dialog is dismissible.
    expect(inPortal(".pl-dialog__close")).not.toBeNull();

    pressEscape();
    expect(onClose).toHaveBeenCalledTimes(1);

    clickBackdrop();
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("a Dialog WITHOUT onClose is non-dismissible (no × button, Escape + backdrop are inert)", () => {
    mount(h(Dialog, { open: true, title: "Blocking" }, "body"));

    // No dismiss affordance, and the dialog stays mounted through Escape + backdrop.
    expect(inPortal(".pl-dialog__close")).toBeNull();
    pressEscape();
    clickBackdrop();
    expect(inPortal(".pl-dialog")).not.toBeNull();
  });
});

// DS 0.63 card 3b: content dialogs opt into `padding="roomy"` so the body keeps its 24px
// padding once the app-wide `.pl-dialog__body` rule is deleted by a later card. This pins the
// DS contract the opt-in rides on — `"roomy"` adds the `--roomy` body modifier; omitting it
// does not — so a content dialog missing the prop would be caught downstream.
describe("DS Dialog padding contract (card 3b)", () => {
  it('padding="roomy" adds the roomy body modifier', () => {
    mount(h(Dialog, { open: true, title: "Roomy", padding: "roomy" }, "body"));
    expect(inPortal(".pl-dialog__body--roomy")).not.toBeNull();
  });

  it("omitting padding leaves the DS-default body (no roomy modifier)", () => {
    mount(h(Dialog, { open: true, title: "Default" }, "body"));
    expect(inPortal(".pl-dialog__body")).not.toBeNull();
    expect(inPortal(".pl-dialog__body--roomy")).toBeNull();
  });
});

describe("AuthGate — blocking auth modal (#1921)", () => {
  beforeEach(() => notifyAuthRequired());
  afterEach(() => clearAuthRequired());

  function mountGate() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mount(h(QueryClientProvider, { client: qc }, h(AuthGate)));
  }

  it("renders no dismiss affordances — no × close, no 'Not now', only Connect", () => {
    mountGate();
    expect(inPortal(".pl-dialog")).not.toBeNull();
    expect(inPortal(".pl-dialog__close")).toBeNull();
    expect(buttonByText("Not now")).toBeUndefined();
    expect(buttonByText("Connect")).toBeDefined();
  });

  it("opts into roomy DS Dialog body padding (card 3b)", () => {
    mountGate();
    expect(inPortal(".pl-dialog__body--roomy")).not.toBeNull();
  });

  it("does not close on Escape or backdrop click — the auth state persists", () => {
    mountGate();
    pressEscape();
    clickBackdrop();
    expect(inPortal(".pl-dialog")).not.toBeNull();
    expect(authRequired()).toBe(true); // still gated — the only exit is authenticating
  });
});

// "Have a pairing code?" (ADR 0087 D3 amendment, #4336 S2): a phone that can't scan types the
// short device code here instead of an operator token. Same blocking dialog, second mode.
describe("AuthGate — typed pairing code", () => {
  beforeEach(() => {
    window.localStorage.clear();
    notifyAuthRequired();
  });
  afterEach(() => {
    clearAuthRequired();
    vi.unstubAllGlobals();
  });

  function mountGate() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mount(h(QueryClientProvider, { client: qc }, h(AuthGate)));
  }

  function codeInput() {
    const el = inPortal<HTMLInputElement>('[data-testid="auth-pair-code"]');
    if (!el) throw new Error("no pairing-code input");
    return el;
  }

  function type(el: HTMLInputElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    act(() => {
      setter.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  async function click(text: string) {
    const b = buttonByText(text);
    if (!b) throw new Error(`no button ${text}`);
    await act(async () => {
      b.click();
    });
  }

  it("switches to a code field and back without leaving the blocking dialog", async () => {
    mountGate();
    await click("Have a pairing code?");
    expect(codeInput()).toBeTruthy();
    expect(inPortal(".pl-dialog__close")).toBeNull(); // still non-dismissible
    await click("Use a token instead");
    expect(inPortal('[data-testid="auth-pair-code"]')).toBeNull();
    expect(buttonByText("Connect")).toBeDefined();
  });

  it("formats as typed and only enables Pair once the code is complete", async () => {
    mountGate();
    await click("Have a pairing code?");
    type(codeInput(), "k7qm2");
    expect(codeInput().value).toBe("K7QM2");
    expect(buttonByText("Pair this device")?.disabled).toBe(true);
    type(codeInput(), "k7qm2 xpa4f");
    expect(codeInput().value).toBe("K7QM2-XPA4F");
    expect(buttonByText("Pair this device")?.disabled).toBe(false);
  });

  it("claims, stores the token where a #pair= link would, and lifts the gate", async () => {
    const fetchSpy = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, token: "device-token", device: { id: "dev9" } }),
    }));
    vi.stubGlobal("fetch", fetchSpy);
    mountGate();
    await click("Have a pairing code?");
    type(codeInput(), "K7QM2-XPA4F");
    await click("Pair this device");
    const [url, init] = fetchSpy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/pairing/claim");
    expect(JSON.parse(String(init.body)).code).toBe("K7QM2-XPA4F");
    // The device's own key (#4336 S3), never the operator's.
    expect(window.localStorage.getItem("protoagent.deviceToken")).toBe("device-token");
    expect(window.localStorage.getItem("protoagent.authToken")).toBeNull();
    expect(window.localStorage.getItem("protoagent.deviceId")).toBe("dev9");
    expect(authRequired()).toBe(false);
  });

  it("a rejected code reads as an actionable message and keeps the gate up", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 403, json: async () => ({ error: "invalid or expired pairing code" }) })),
    );
    mountGate();
    await click("Have a pairing code?");
    type(codeInput(), "ZZZZZZZZZZ");
    await click("Pair this device");
    const alert = inPortal('[role="alert"]');
    expect(alert?.textContent).toMatch(/didn't work/);
    expect(alert?.textContent).toMatch(/expired/);
    expect(alert?.textContent).toMatch(/new one/);
    expect(alert?.textContent).not.toMatch(/HTTP 403/);
    expect(authRequired()).toBe(true);
    expect(window.localStorage.getItem("protoagent.authToken")).toBeNull();
  });

  it("never sends an incomplete code (each miss spends a lockout guess)", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    mountGate();
    await click("Have a pairing code?");
    type(codeInput(), "K7QM2-XP");
    const form = codeInput().closest("form")!;
    await act(async () => {
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
