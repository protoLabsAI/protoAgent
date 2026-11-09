import { act, createElement as h, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DecisionRow } from "../lib/api/decisions";

// The two entry points read the fleet-wide useDecisions() singleton; drive it by hand so the
// tests exercise the composites' rendering (labels, badge, controlled open, focus return) without
// the store's network/bus. Integration cases use the real store with API/event boundaries mocked.
// createRoot + act, like the other console UI suites — no RTL dep.
const store = vi.hoisted(() => ({
  real: false,
  openPanel: vi.fn(),
  closePanel: vi.fn(),
  answer: vi.fn(),
  snap: {
    state: "loading",
    decisions: [] as DecisionRow[],
    pendingCount: 0,
    top: null as DecisionRow | null,
    loaded: false,
    loadedAt: null as number | null,
    panelOpen: false,
  },
}));
vi.mock("../lib/events", () => ({
  onTopic: () => () => {},
  onConnectionChange: () => () => {},
}));
vi.mock("../lib/notify", () => ({ notifyIfHidden: vi.fn() }));
vi.mock("./useDecisions", async (importOriginal) => {
  const original = await importOriginal<typeof import("./useDecisions")>();
  return {
    ...original,
    useDecisions: () => store.real ? original.useDecisions() : ({
      ...store.snap,
      openPanel: store.openPanel,
      closePanel: store.closePanel,
      answer: store.answer,
      refresh: () => {},
      cancel: () => {},
    }),
  };
});

import { decisionsApi } from "../lib/api/decisions";
import { decisionsStoreForTest } from "./useDecisions";

import { UtilityWidget } from "../app/UtilityWidget";
import { DecisionsHeaderButton } from "./DecisionsHeaderButton";
import {
  DecisionsWidget,
  DECISIONS_DIALOG_WIDTH,
  decisionsInfo,
  decisionsLabel,
} from "./DecisionsWidget";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const decision = (id: string, patch: Partial<DecisionRow> = {}): DecisionRow => ({
  id,
  session_id: "s1",
  agent: "protoEngineer",
  question: "Merge the release?",
  options: [
    { id: "merge", label: "Merge with the WARN", recommended: true },
    { id: "hold", label: "Hold the release" },
  ],
  created_at: "2026-11-08T14:45:00+00:00",
  state: "pending",
  answer: null,
  answered_at: null,
  expires_at: null,
  default_option: null,
  thread_id: "t1",
  interrupt_id: "i1",
  context: "",
  default_action: null,
  withdraw_reason: null,
  answered_by: null,
  answered_surface: null,
  default_action_text: "carry on without this step",
  answer_label: null,
  ...patch,
});

const DEFAULT_SNAP = { ...store.snap };
function setSnap(patch: Partial<typeof store.snap>) {
  store.snap = { ...store.snap, ...patch };
}

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  store.real = false;
  store.snap = { ...DEFAULT_SNAP };
  store.openPanel.mockReset();
  store.closePanel.mockReset();
  store.answer.mockReset();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  decisionsStoreForTest.closePanel();
  vi.restoreAllMocks();
  host.remove();
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

async function render(node: ReactNode) {
  await act(async () => {
    root.render(node);
  });
}
const pill = () => document.querySelector<HTMLButtonElement>('[data-testid="decisions-widget"]');
const headBtn = () => document.querySelector<HTMLButtonElement>("button.decisions-head-btn");

// ── UtilityWidget: controlled open + aria-haspopup (Q11, Q12) ────────────────────────────────
describe("UtilityWidget controlled/uncontrolled open", () => {
  const base = { testId: "util-widget", label: "Inbox", dialogTitle: "Inbox", icon: h("span", null, "i") };
  const util = () => document.querySelector<HTMLButtonElement>('[data-testid="util-widget"]');

  it("stays uncontrolled by default, exposes aria-haspopup=dialog, toggles aria-expanded", async () => {
    await render(h(UtilityWidget, { ...base, children: h("p", { "data-testid": "body" }, "x") }));
    expect(util()!.getAttribute("aria-haspopup")).toBe("dialog");
    expect(util()!.getAttribute("aria-expanded")).toBe("false");
    // No open prop → it drives its own state: the click opens the dialog.
    expect(document.querySelector('[data-testid="body"]')).toBeNull();
    await act(async () => util()!.click());
    expect(document.querySelector('[data-testid="body"]')).not.toBeNull();
    expect(util()!.getAttribute("aria-expanded")).toBe("true");
    await act(async () => document.querySelector<HTMLButtonElement>(".pl-dialog__close")!.click());
    expect(document.querySelector('[data-testid="body"]')).toBeNull();
    expect(util()!.getAttribute("aria-expanded")).toBe("false");
  });

  it("obeys the controlled open prop and reports every change via onOpenChange", async () => {
    const onOpenChange = vi.fn();
    await render(
      h(UtilityWidget, { ...base, open: false, onOpenChange, children: h("p", { "data-testid": "body" }, "x") }),
    );
    // Controlled + closed: the click asks the owner to open but does NOT open on its own.
    await act(async () => util()!.click());
    expect(onOpenChange).toHaveBeenLastCalledWith(true);
    expect(document.querySelector('[data-testid="body"]')).toBeNull();
    expect(util()!.getAttribute("aria-expanded")).toBe("false");
    // The owner flips the prop → now it opens, and closing reports false back.
    await render(
      h(UtilityWidget, { ...base, open: true, onOpenChange, children: h("p", { "data-testid": "body" }, "x") }),
    );
    expect(document.querySelector('[data-testid="body"]')).not.toBeNull();
    expect(util()!.getAttribute("aria-expanded")).toBe("true");
    const close = document.querySelector<HTMLButtonElement>(".pl-dialog__close");
    await act(async () => close!.click());
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
  });
});

describe("entry points with the shared Decisions store", () => {
  beforeEach(() => {
    store.real = true;
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue({
      decisions: [decision("shared")],
      pending: 1,
    });
  });

  it("opens the desktop dialog via openPanel without clicking the pill", async () => {
    await render(h(DecisionsWidget));
    expect(pill()!.getAttribute("aria-expanded")).toBe("false");
    await act(async () => decisionsStoreForTest.openPanel());
    expect(pill()!.getAttribute("aria-expanded")).toBe("true");
    expect(document.querySelector('[role="dialog"]')!.textContent).toContain("Merge the release?");
    await act(async () => document.querySelector<HTMLButtonElement>(".pl-dialog__close")!.click());
    expect(decisionsStoreForTest.getSnapshot().panelOpen).toBe(false);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("opens and dismisses the phone sheet through the store and restores focus", async () => {
    await render(h(DecisionsHeaderButton));
    const button = headBtn()!;
    await act(async () => button.click());
    expect(decisionsStoreForTest.getSnapshot().panelOpen).toBe(true);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(decisionsStoreForTest.getSnapshot().panelOpen).toBe(false);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(button);
  });
});

// ── decisionsLabel / decisionsInfo copy (§4.1) ───────────────────────────────────────────────
describe("DecisionsWidget copy helpers", () => {
  it("builds the accessible label for none / one / many", () => {
    expect(decisionsLabel(0)).toBe("Decisions, nothing waiting");
    expect(decisionsLabel(1)).toBe("Decisions, 1 waiting on you");
    expect(decisionsLabel(4)).toBe("Decisions, 4 waiting on you");
  });

  it("builds the hover info: quiet line, top question, and an 'and N−1 more' tail", async () => {
    await render(h("div", null, decisionsInfo(null, 0)));
    expect(host.textContent).toContain("Decisions · nothing is waiting on you");

    await render(h("div", null, decisionsInfo(decision("a"), 1)));
    expect(host.textContent).toContain("protoEngineer:");
    expect(host.textContent).toContain("Merge the release?");
    expect(host.textContent).not.toContain("more");

    await render(h("div", null, decisionsInfo(decision("a"), 3)));
    expect(host.textContent).toContain("and 2 more");
  });

  it("cuts the preview question at 120 characters with an ellipsis", async () => {
    const long = "x".repeat(200);
    await render(h("div", null, decisionsInfo(decision("a", { question: long }), 1)));
    expect(host.textContent).toContain("…");
    expect(host.textContent).not.toContain("x".repeat(130));
    expect((host.textContent ?? "").includes("x".repeat(120))).toBe(true);
  });
});

// ── DecisionsWidget: the desktop pill ────────────────────────────────────────────────────────
describe("DecisionsWidget", () => {
  it("is quiet (icon, no badge) with nothing pending and before the first load", async () => {
    // Before the first load.
    await render(h(DecisionsWidget));
    expect(pill()).not.toBeNull();
    expect(pill()!.querySelector(".pl-count")).toBeNull();
    expect(pill()!.getAttribute("aria-label")).toBe("Decisions, nothing waiting");
    setSnap({ pendingCount: 2, top: decision("a") });
    await render(h(DecisionsWidget));
    expect(pill()!.querySelector(".pl-count")).toBeNull();
    expect(pill()!.getAttribute("aria-label")).toBe("Decisions, nothing waiting");
    // Loaded, still nothing pending — still quiet, never "0".
    setSnap({ loaded: true, state: "ready", pendingCount: 0 });
    await render(h(DecisionsWidget));
    expect(pill()!.querySelector(".pl-count")).toBeNull();
    expect(pill()!.textContent).not.toContain("0");
  });

  it("carries a default Count while pending, 9+ above 9, with the §4.1 label", async () => {
    setSnap({ loaded: true, state: "ready", pendingCount: 1, top: decision("a"), decisions: [decision("a")] });
    await render(h(DecisionsWidget));
    const badge = pill()!.querySelector(".pl-count");
    expect(badge).not.toBeNull();
    expect(badge!.textContent).toBe("1");
    // Never the alert/warning variant — a request, not an alarm.
    expect(badge!.className).not.toContain("alert");
    expect(badge!.className).not.toContain("warning");
    expect(pill()!.getAttribute("aria-label")).toBe("Decisions, 1 waiting on you");

    setSnap({ pendingCount: 12 });
    await render(h(DecisionsWidget));
    expect(pill()!.querySelector(".pl-count")!.textContent).toBe("9+");
    expect(pill()!.getAttribute("aria-label")).toBe("Decisions, 12 waiting on you");
  });

  it("opens the DecisionsPanel dialog from the store's panelOpen, at the spec width", async () => {
    expect(DECISIONS_DIALOG_WIDTH).toBe("min(520px, 94vw)");
    setSnap({ loaded: true, state: "ready", pendingCount: 1, top: decision("a"), decisions: [decision("a")] });
    await render(h(DecisionsWidget));
    // Closed: the body mounts only while open.
    expect(document.body.textContent).not.toContain("Your agents stopped to ask you these.");
    // Clicking asks the store to open (controlled by panelOpen).
    await act(async () => pill()!.click());
    expect(store.openPanel).toHaveBeenCalledTimes(1);
    // The store flips panelOpen → the dialog opens with the panel body at the given width.
    setSnap({ panelOpen: true });
    await render(h(DecisionsWidget));
    const dialog = document.querySelector<HTMLElement>(".pl-dialog");
    expect(dialog).not.toBeNull();
    expect(document.body.textContent).toContain("Your agents stopped to ask you these.");
    // jsdom drops the min() width but keeps the companion max-width, proving a width flowed through.
    expect(dialog!.getAttribute("style") ?? "").toContain("max-width: 100%");
  });
});

// ── DecisionsHeaderButton: the phone entry point ─────────────────────────────────────────────
describe("DecisionsHeaderButton", () => {
  it("renders nothing while the count is 0 or before the first load", async () => {
    await render(h(DecisionsHeaderButton));
    expect(headBtn()).toBeNull();
    // Count > 0 but not loaded yet (first load) — still hidden.
    setSnap({ pendingCount: 2 });
    await render(h(DecisionsHeaderButton));
    expect(headBtn()).toBeNull();
    // Loaded with a pending decision — now it appears.
    setSnap({ loaded: true, state: "ready" });
    await render(h(DecisionsHeaderButton));
    expect(headBtn()).not.toBeNull();
    setSnap({ pendingCount: 0 });
    await render(h(DecisionsHeaderButton));
    expect(headBtn()).toBeNull();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("shows the icon + a Count beside it, both aria-hidden, with the §4.1 name and aria-haspopup", async () => {
    setSnap({ loaded: true, state: "ready", pendingCount: 2, decisions: [decision("a")] });
    await render(h(DecisionsHeaderButton));
    const btn = headBtn()!;
    expect(btn.getAttribute("aria-label")).toBe("Decisions, 2 waiting on you");
    expect(btn.getAttribute("title")).toBe("Decisions waiting on you");
    expect(btn.getAttribute("aria-haspopup")).toBe("dialog");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    const badge = btn.querySelector(".pl-count")!;
    expect(badge.textContent).toBe("2");
    expect(badge.getAttribute("aria-hidden")).toBe("true");
    expect(btn.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");

    setSnap({ pendingCount: 20 });
    await render(h(DecisionsHeaderButton));
    expect(headBtn()!.querySelector(".pl-count")!.textContent).toBe("9+");
  });

  it("carries a token-only DS focus-visible ring (Josh's composite-trigger rule)", async () => {
    setSnap({ loaded: true, state: "ready", pendingCount: 1, decisions: [decision("a")] });
    await render(h(DecisionsHeaderButton));
    expect(headBtn()!.classList.contains("decisions-head-btn")).toBe(true);
    const css = [...document.querySelectorAll("style")].map((s) => s.textContent ?? "").join("\n");
    expect(css).toContain(".decisions-head-btn:focus-visible");
    expect(css).toContain("outline: var(--pl-border-width) solid var(--pl-color-fg)");
  });

  it("opens the bottom sheet and reflects it in aria-expanded", async () => {
    setSnap({ loaded: true, state: "ready", pendingCount: 1, decisions: [decision("a")] });
    await render(h(DecisionsHeaderButton));
    expect(document.querySelector(".pl-drawer--bottom")).toBeNull();
    await act(async () => headBtn()!.click());
    expect(store.openPanel).toHaveBeenCalledTimes(1);
    setSnap({ panelOpen: true });
    await render(h(DecisionsHeaderButton));
    expect(document.querySelector(".pl-drawer--bottom")).not.toBeNull();
    expect(headBtn()!.getAttribute("aria-expanded")).toBe("true");
  });

  it("returns focus to the button on close", async () => {
    setSnap({ loaded: true, state: "ready", pendingCount: 1, decisions: [decision("a")] });
    await render(h(DecisionsHeaderButton));
    const btn = headBtn()!;
    setSnap({ panelOpen: true });
    await render(h(DecisionsHeaderButton));
    setSnap({ panelOpen: false });
    await render(h(DecisionsHeaderButton));
    expect(document.activeElement).toBe(btn);
  });

  it("falls back to New chat when the button has gone (last decision cleared, §5.1)", async () => {
    const newChat = document.createElement("button");
    newChat.textContent = "New chat";
    document.body.append(newChat);
    const fallbackFocus = { current: newChat as HTMLElement };

    setSnap({ loaded: true, state: "ready", pendingCount: 1, decisions: [decision("a")], panelOpen: true });
    await render(h(DecisionsHeaderButton, { fallbackFocus }));
    expect(headBtn()).not.toBeNull();
    // The last decision clears while the sheet is open — the button unmounts, the sheet stays.
    setSnap({ pendingCount: 0, decisions: [] });
    await render(h(DecisionsHeaderButton, { fallbackFocus }));
    expect(headBtn()).toBeNull();
    expect(document.querySelector(".pl-drawer--bottom")).not.toBeNull();
    // Closing the sheet now returns focus to New chat.
    setSnap({ panelOpen: false });
    await render(h(DecisionsHeaderButton, { fallbackFocus }));
    expect(document.activeElement).toBe(newChat);
    newChat.remove();
  });
});
