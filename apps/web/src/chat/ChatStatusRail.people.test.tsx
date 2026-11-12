/** People on the chat status rail (v2 S6b, the v7 behavior): one measured row — draft-first
 *  order, `+K` for whoever doesn't fit (fallback caps until ResizeObserver delivers), and the
 *  picker ("In this chat · N") that toggles leading-run mentions without touching the body.
 *  Group chips are covered in ChatStatusRail.test.tsx; here only their counts' independence
 *  from people visibility is asserted. */
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkItems } from "../work/useWorkItems";
import { ChatStatusRail } from "./ChatStatusRail";
import { toggleMention } from "./mentionRun";
import { fitCount, orderPeople, recipientLine, spokenMoreLabel } from "./peopleFit";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const state = vi.hoisted(() => ({ mobile: false, sources: null as unknown as WorkItems }));
vi.mock("../lib/useIsMobile", () => ({ useIsMobile: () => state.mobile }));
vi.mock("../work/useWorkItems", () => ({ useWorkItems: () => state.sources }));
vi.mock("./useDirectives", () => ({
  useDirectives: () => ({ state: "ready", loadedAt: 1, refresh: vi.fn(), active: [], history: [], revoke: vi.fn() }),
}));

/** `running` jobs for this chat: 1 → the "Doing now 1" status chip, 0 → a chip-less rail. */
function mkSources(running: number): WorkItems {
  const src = () => ({ items: [], status: "ready", loadedAt: 1, error: null, retry: vi.fn() });
  const items = Array.from({ length: running }, (_, i) => ({
    kind: "background", id: `bg-${i}`, status: "running", title: `Job ${i}`, meta: "", sessionId: "s1",
    raw: { status: "running" },
  }));
  return {
    decisions: src(), goals: src(), watches: src(), tasks: src(), schedules: src(), activity: src(),
    background: { ...src(), groupItems: items }, directives: { ...src(), groupItems: [] },
  } as unknown as WorkItems;
}

/** The real wiring (ChatSessionSlot): a controlled draft the toggles edit via toggleMention. */
const calls: Array<[string, string | undefined]> = [];
function Harness({ cast, initial = "" }: { cast: string[]; initial?: string }) {
  const [draft, setDraft] = useState(initial);
  return (
    <ChatStatusRail sessionId="s1" cast={cast} draft={draft}
      onToggleMention={(name, source) => { calls.push([name, source]); setDraft((d) => toggleMention(d, name)); }} />
  );
}

class FakeRO {
  static all: FakeRO[] = [];
  el: Element | null = null;
  constructor(readonly cb: (entries: unknown[]) => void) { FakeRO.all.push(this); }
  observe(el: Element) { this.el = el; }
  unobserve() {}
  disconnect() {}
  fire(width: number) { this.cb([{ target: this.el, contentRect: { width } }]); }
}

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  Object.assign(state, { mobile: false, sources: mkSources(0) });
  calls.length = FakeRO.all.length = 0;
  vi.stubGlobal("ResizeObserver", FakeRO);
  Element.prototype.hasPointerCapture = Element.prototype.hasPointerCapture ?? (() => false);
  Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

const render = (node: Parameters<Root["render"]>[0]) => act(async () => root.render(node));
const names = () => [...host.querySelectorAll("button.chat-roster-chip:not([data-testid])")].map((b) => b.textContent);
const more = () => host.querySelector<HTMLButtonElement>('[data-testid="chat-roster-more"]');
const click = (el: Element | null | undefined) => act(async () => (el as HTMLElement).click());
const CAST = ["ana", "bo", "cy", "dee", "elle", "fay"];

// Only the edges the rendered rail can't reach — everything the rail CAN show (the
// fallback caps, the measured refit, the live recipients line) is proven once, below,
// through the rendered row (docs/dev/testing.md: one layer per behavior).
describe("peopleFit (pure edges)", () => {
  it("orders draft-addressed first (run order, case-insensitive), ignoring non-cast names", () => {
    expect(orderPeople(CAST, "@FAY @cy @ghost hi")).toEqual(["fay", "cy", "ana", "bo", "dee", "elle"]);
    expect(orderPeople(CAST, "no run")).toEqual(CAST);
  });
  it("fit boundaries: an exact right edge fits everyone; too narrow for one name is +K alone", () => {
    const boxes = [60, 120, 180, 240].map((left) => ({ left, width: 50 }));
    expect([fitCount(boxes, 50, 290), fitCount(boxes, 50, 80)]).toEqual([4, 0]);
  });
  it("recipients: no line when the run addresses nobody; a non-cast name keeps its typed spelling", () => {
    expect(recipientLine("", CAST)).toBeNull();
    expect(recipientLine("@BO @Vera hi", CAST)).toBe(
      "Your message goes to @bo, @Vera. Choose a name again to take it out.");
    // The one exact pin of the +K spoken words (the rendered checks reuse the helper).
    expect(spokenMoreLabel(1)).toBe("1 more person in this chat. Show everyone");
  });
});

describe("the one-row fit", () => {
  it.each([
    [false, 1, ["@ana", "@bo", "@cy"], "+3"], // desktop beside status chips: 3
    [false, 0, ["@ana", "@bo", "@cy", "@dee", "@elle"], "+1"], // desktop, people own the row: 5
    [true, 1, ["@ana"], "+5"], // phone beside status chips: 1
    [true, 0, ["@ana", "@bo", "@cy"], "+3"], // phone, people own the row: 3
  ])("unmeasured fallback (mobile=%s, running=%s) → %j then %s", async (mobile, running, visible, k) => {
    Object.assign(state, { mobile, sources: mkSources(running) });
    await render(<Harness cast={CAST} />);
    expect(names()).toEqual(visible);
    expect(more()!.textContent).toBe(k);
    expect(more()!.getAttribute("aria-label")).toBe(spokenMoreLabel(CAST.length - visible.length));
    // The status chip's count comes from the shared selector — people visibility never moves it.
    const doing = host.querySelector('[data-testid="chat-status-chip-doing-now"]');
    expect(doing?.querySelector(".chat-status-chip__count")?.textContent ?? null).toBe(running ? "1" : null);
  });

  it("shows everyone, +K-less, when the cast fits the cap", async () => {
    await render(<Harness cast={CAST.slice(0, 3)} />);
    expect(names()).toEqual(["@ana", "@bo", "@cy"]);
    expect(more()).toBeNull();
  });

  it("uses the measured mirror row once ResizeObserver delivers, and refits on resize", async () => {
    // Mirror-row children at 60px pitch, 50px wide (the icon is child 0, chips from 60 on).
    const offsets = {
      offsetLeft: { configurable: true, get(this: HTMLElement) { return [...this.parentElement!.children].indexOf(this) * 60; } },
      offsetWidth: { configurable: true, get: () => 50 },
    };
    const plain = Object.fromEntries(Object.keys(offsets).map(
      (key) => [key, Object.getOwnPropertyDescriptor(HTMLElement.prototype, key)]));
    Object.defineProperties(HTMLElement.prototype, offsets);
    try {
      await render(<Harness cast={CAST.slice(0, 4)} />);
      const ro = FakeRO.all.find((o) => o.el?.classList.contains("chat-roster"))!;
      await act(async () => ro.fire(200)); // chips end at 290: only 1 + "+3" leave room
      expect([names(), more()!.textContent]).toEqual([["@ana"], "+3"]);
      await act(async () => ro.fire(400)); // everyone fits: no +K
      expect([names(), more()]).toEqual([["@ana", "@bo", "@cy", "@dee"], null]);
    } finally {
      // Restore jsdom's OWN descriptors (not bare deletes — jsdom defines both getters).
      for (const [key, desc] of Object.entries(plain)) {
        if (desc) Object.defineProperty(HTMLElement.prototype, key, desc);
        else Reflect.deleteProperty(HTMLElement.prototype, key);
      }
    }
  });
});

describe("the +K picker", () => {
  it("opens at In this chat · N, keeps picking open, reorders the rail, and restores focus", async () => {
    state.sources = mkSources(1);
    await render(<Harness cast={CAST} initial="@bo hi there" />);
    expect(names()).toEqual(["@bo", "@ana", "@cy"]); // draft-addressed first, then cast order
    expect(more()!.getAttribute("aria-haspopup")).toBe("dialog");
    await click(more());
    expect(more()!.getAttribute("aria-expanded")).toBe("true");
    const panel = document.querySelector<HTMLElement>('[role="dialog"] .chat-people')!;
    expect(panel.querySelector("h2")!.getAttribute("aria-label")).toBe("In this chat, 6");
    const chips = [...panel.querySelectorAll<HTMLButtonElement>("button.chat-roster-chip")];
    expect(chips.map((c) => [c.textContent, c.getAttribute("aria-pressed")]))
      .toEqual(CAST.map((n) => [`@${n}`, String(n === "bo")]));
    await click(chips.find((c) => c.textContent === "@fay"));
    // Toggled from the picker (never the composer-focusing rail path), body untouched,
    // panel still open, +K count unmoved — and the pick LEADS the rail (it joins the
    // run's end, where a full row would hide it; the recipients line keeps run order).
    expect(calls).toEqual([["fay", "picker"]]);
    expect(names()).toEqual(["@fay", "@bo", "@ana"]);
    const fay = document.querySelector<HTMLElement>('.chat-people button[title="Remove @fay from this message"]')!;
    expect([fay.getAttribute("aria-pressed"), more()!.textContent]).toEqual(["true", "+3"]);
    expect(document.querySelector(".chat-people__line")!.textContent)
      .toBe("Your message goes to @bo, @fay. Choose a name again to take it out.");
    await click(fay); // picking again takes it out; the picker still stays open
    expect(document.querySelector(".chat-people__line")!.textContent).toContain("goes to @bo.");
    await act(async () => document.querySelector(".chat-status-details")!
      .dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.querySelector(".chat-people")).toBeNull();
    expect([document.activeElement, more()!.getAttribute("aria-expanded")]).toEqual([more(), "false"]);
  });

  it("adds a People section to the grouped details ONLY while names are hidden", async () => {
    state.sources = mkSources(1);
    await render(<Harness cast={CAST} />);
    await click(host.querySelector('[data-testid="chat-status-chip-doing-now"]'));
    expect(document.querySelector('[role="dialog"] .chat-people h2')!.textContent).toBe("In this chat · 6");
    // Everyone becomes visible under the open panel: the section leaves, the panel stays.
    await render(<Harness cast={CAST.slice(0, 2)} />);
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    expect(document.querySelector('[role="dialog"] .chat-people')).toBeNull();
  });

  it("phone: a pick takes the ONE visible slot, and close returns focus to +K", async () => {
    Object.assign(state, { mobile: true, sources: mkSources(1) });
    await render(<Harness cast={CAST} initial="@bo hi" />);
    expect(names()).toEqual(["@bo"]); // cap 1 beside the status chip: addressed names fill the row
    await click(more());
    const sheet = document.querySelector<HTMLElement>(".pl-drawer--bottom")!;
    expect(sheet.querySelectorAll(".chat-people button.chat-roster-chip")).toHaveLength(6);
    expect(sheet.querySelector(".chat-status-details__section h2")!.textContent).toBe("In this chat · 6");
    // The pick joins the run's END — only its promotion wins it the one visible slot.
    await click([...sheet.querySelectorAll("button.chat-roster-chip")].find((b) => b.textContent === "@fay"));
    expect([names(), more()!.textContent]).toEqual([["@fay"], "+5"]);
    await click(sheet.querySelector('button[title="Remove @fay from this message"]'));
    expect(names()).toEqual(["@bo"]); // unpicking ends the promotion: back to run order
    await click(sheet.querySelector('[aria-label="Close"]'));
    expect([document.querySelector(".pl-drawer--bottom"), document.activeElement]).toEqual([null, more()]);
  });
});
