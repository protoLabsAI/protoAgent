/** The rail's fleet Needs-you marker + decision answer states (work consolidation v2 S7),
 *  counted AGAINST the shared selector + shared-record overlay so the rail and Work cannot
 *  drift. The DecisionCard state machine is decisions.test.tsx's; layout/overflow is
 *  chat-status-rail-needs-you.spec.ts's; the store's notify-once latch (across chats/reload)
 *  is decisions.test.tsx "notifies once per new pending id" — the rail adds no notifier. */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DecisionRow } from "../lib/api/decisions";
import type { InboxItem } from "../lib/types";
import type { WorkItems, WorkSource } from "../work/useWorkItems";
import { flattenWorkItems, withSharedDecisions, workGroups } from "../work/workGroups";
import type { GroupableWorkItem } from "../work/tone";
import { ChatStatusRail, needsYouSpokenLabel } from "./ChatStatusRail";
import { fallbackCap } from "./peopleFit";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const state = vi.hoisted(() => ({ mobile: false, sources: null as unknown as WorkItems }));
const dec = vi.hoisted(() => ({
  records: {} as Record<string, DecisionRow>,
  missing: {} as Record<string, true>,
  answer: vi.fn(),
  recheck: vi.fn(),
  openChat: vi.fn(),
  rearm: vi.fn(),
  clear: vi.fn(),
}));
vi.mock("../lib/useIsMobile", () => ({ useIsMobile: () => state.mobile }));
vi.mock("../work/useWorkItems", () => ({ useWorkItems: () => state.sources }));
vi.mock("./useDirectives", () => ({
  useDirectives: () => ({ state: "ready", loadedAt: 1, refresh: vi.fn(), active: [], history: [], revoke: vi.fn() }),
}));
vi.mock("../decisions/useDecisions", () => ({
  useDecisions: () => ({ records: dec.records, missing: dec.missing, answer: dec.answer }),
  recheckDecision: (id: string) => dec.recheck(id),
  dismissDecision: vi.fn(),
}));
vi.mock("../work/openSourceChat", () => ({
  openSourceChat: (id: string, reply?: boolean) => dec.openChat(id, reply),
}));
vi.mock("../lib/api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api")>();
  return { ...mod, api: { ...mod.api, rearmGoal: (...a: unknown[]) => dec.rearm(...a), clearGoal: (...a: unknown[]) => dec.clear(...a) } };
});

const SESSION = "s1";
// Only the fields the card reads; the omitted nullable wire fields read as absent.
const row = (id: string, extra: Partial<DecisionRow> = {}): DecisionRow => ({
  id, session_id: `chat-${id}`, agent: "scout", question: `Decide ${id}`, state: "pending",
  options: [{ id: "a", label: "Go", recommended: true }, { id: "b", label: "Wait" }],
  created_at: new Date().toISOString(), default_action_text: "carry on without this step",
  ...extra,
} as DecisionRow);
const decision = (r: DecisionRow): GroupableWorkItem => ({
  kind: "decision", id: r.id, status: "needs-you", title: r.question, meta: "", sessionId: r.session_id, raw: r,
});
const delivery = (id: number): GroupableWorkItem => ({
  kind: "delivery", id: String(id), status: "needs-you", title: `Page ${id}`, meta: "mail",
  // Operator-class (needs_human): a merely queued page retries on its own and never Needs-you.
  raw: { id, text: `Page ${id}`, source: "mail", priority: "now", delivered_at: null, needs_human: true } as InboxItem,
});
const stoppedGoal = (id: string): GroupableWorkItem => ({
  kind: "goal", id, status: "failed", title: `Goal ${id}`, meta: "", sessionId: id,
  raw: { session_id: id, status: "exhausted" },
});
function mkSources(items: GroupableWorkItem[]): WorkItems {
  const src = (): WorkSource => ({ items: [], status: "ready", loadedAt: 1, error: null, retry: vi.fn() });
  return {
    decisions: src(), goals: src(), watches: src(), tasks: src(), schedules: src(), activity: src(),
    background: { ...src(), groupItems: items }, directives: { ...src(), groupItems: [] },
  };
}

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.clearAllMocks();
  Object.assign(state, { mobile: false });
  dec.records = {};
  dec.missing = {};
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
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

async function render(node: ReactElement) {
  await act(async () => root.render(node));
}
const rail = (items: GroupableWorkItem[]) => {
  state.sources = mkSources(items);
  return <ChatStatusRail sessionId={SESSION} cast={[]} draft="" onToggleMention={() => {}} />;
};
const marker = () => host.querySelector<HTMLButtonElement>('[data-testid="chat-status-chip-needs-you"]');
const markerCount = () => marker()?.querySelector(".chat-status-chip__count")?.textContent ?? null;
const panel = () => document.querySelector<HTMLElement>(".chat-status-details");
const section = () => document.querySelector<HTMLElement>(".chat-needs-you");
async function click(el: Element | null | undefined) {
  expect(el).toBeTruthy();
  await act(async () => (el as HTMLElement).click());
}
const button = (label: string, scope: ParentNode | null = section()) =>
  [...(scope ?? document).querySelectorAll("button")].find((b) => b.textContent === label);
/** The shared projection the component must agree with — selector + shared-record overlay. */
const expectedCount = () => workGroups(
  flattenWorkItems(withSharedDecisions(state.sources, { records: dec.records, missing: dec.missing })),
  { thisChatOnly: true, sessionId: SESSION },
).find((g) => g.id === "needs-you")!.count;

describe("the fleet-wide marker", () => {
  it("counts exactly the shared Needs-you rows; the panel leads with the section, rows expand on select", async () => {
    const stale = row("q1", { created_at: new Date(Date.now() - 31 * 60_000).toISOString() });
    const items = [decision(stale), decision(row("q2")), delivery(71), stoppedGoal("g1")];
    await render(rail(items));
    expect([markerCount(), expectedCount()]).toEqual(["4", 4]);
    expect(marker()!.getAttribute("aria-label")).toBe(needsYouSpokenLabel(4));
    expect(marker()).toBe(host.querySelector('[data-testid="chat-status-rail"]')!.firstElementChild);
    // The marker adds no second live region — the rail keeps its ONE polite announcer (§6).
    expect([...host.querySelectorAll("[aria-live], [role='status']")]
      .filter((node) => !node.closest("[aria-hidden='true']"))).toHaveLength(1);
    expect(marker()!.getAttribute("aria-haspopup")).toBe("dialog");
    await click(marker());
    expect(marker()!.getAttribute("aria-expanded")).toBe("true");
    expect(panel()!.querySelector("h2")!.textContent).toBe("Needs you · 4");
    // One expanded card (the first decision): recommended option primary + suggested, the
    // ≥30-minute muted hint with NO warning callout, and the exact-time tooltip on the age.
    expect(section()!.querySelectorAll('[role="group"][aria-label="Answer"]')).toHaveLength(1);
    expect(button("Gosuggested")!.className).toContain("pl-btn--primary");
    expect(button("Reply in chat instead")).toBeTruthy();
    expect(section()!.querySelector(".chat-needs-you__stale")!.textContent).toContain("30 minutes");
    expect(section()!.querySelector(".pl-alert")).toBeNull();
    // The other decision: a one-line agent/question/age row that expands on select.
    const rowBtn = section()!.querySelector<HTMLButtonElement>(".chat-needs-you__row")!;
    expect(rowBtn.getAttribute("aria-expanded")).toBe("false");
    expect(rowBtn.textContent).toContain("Decide q2");
    expect(rowBtn.querySelector(".chat-status-details__meta")!.getAttribute("title")).toBeTruthy();
    await click(rowBtn);
    expect(section()!.querySelectorAll('[role="group"][aria-label="Answer"]')).toHaveLength(2);
    // Delivery page and stopped goal are neutral rows, never DecisionCards.
    expect(section()!.textContent).toContain("Page 71");
    expect(section()!.textContent).toContain("couldn't be delivered");
    expect(section()!.textContent).toContain("Goal stopped");
    await act(async () => panel()!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(panel()).toBeNull();
    expect(document.activeElement).toBe(marker()); // Escape returns focus to the marker
    // Settled and 404 records leave through the SAME overlay Work applies — pending-only.
    dec.records = { q1: row("q1", { state: "answered", answer: "a", answer_label: "Go" }) };
    dec.missing = { q2: true };
    await render(rail(items));
    expect([markerCount(), expectedCount()]).toEqual(["2", 2]);
  });

  it("keeps a `waiting` task (ADR 0129 Q1) out of the marker — the section has no task row to act on", async () => {
    const parked: GroupableWorkItem = {
      kind: "task", id: "t-parked", status: "needs-you", title: "Ship the release", meta: "",
      sessionId: "other-chat", raw: { status: "waiting" },
    };
    // The selector sends the parked task to Needs you (Work draws it there), but the rail
    // counts only the kinds the section draws: alone it renders no marker — no rail at all.
    await render(rail([parked]));
    expect(expectedCount()).toBe(1);
    expect(host.querySelector('[data-testid="chat-status-rail"]')).toBeNull();
    // Next to a pending decision the marker, spoken label and panel heading all say 1 — the
    // section's own rows — never the selector's 2, and the task appears nowhere in the panel.
    await render(rail([parked, decision(row("q1"))]));
    expect([markerCount(), expectedCount()]).toEqual(["1", 2]);
    expect(marker()!.getAttribute("aria-label")).toBe(needsYouSpokenLabel(1));
    await click(marker());
    expect(panel()!.querySelector("h2")!.textContent).toBe("Needs you · 1");
    expect(section()!.textContent).not.toContain("Ship the release");
  });

  it("phone: the marker never folds — people fall back to +K alone; the sheet restores focus", async () => {
    state.mobile = true;
    state.sources = mkSources([decision(row("q1"))]);
    await render(<ChatStatusRail sessionId={SESSION} cast={["ana", "bo"]} draft="" onToggleMention={() => {}} />);
    expect([fallbackCap(true, false, true), fallbackCap(false, true, true), fallbackCap(false, false, true)])
      .toEqual([0, 2, 4]);
    expect([...host.querySelectorAll("button.chat-roster-chip:not([data-testid])")]).toHaveLength(0);
    expect(host.querySelector('[data-testid="chat-roster-more"]')!.textContent).toBe("+2");
    await click(marker());
    expect(document.querySelector(".pl-drawer--bottom .chat-needs-you h2")!.textContent).toBe("Needs you · 1");
    await click(document.querySelector('.pl-drawer--bottom [aria-label="Close"]'));
    expect(document.activeElement).toBe(marker());
  });
});

describe("answer states (shared DecisionCard + store, no fork)", () => {
  const items = [decision(row("q1")), decision(row("q2"))];
  const answered = row("q1", { state: "answered", answer: "a", answer_label: "Go", answered_at: new Date().toISOString(), answered_by: "operator" });

  it("consumed success: Sent + Continuing… immediately, held in the OPEN panel at zero, then zero hides", async () => {
    dec.answer.mockResolvedValue({ answered_elsewhere: false, resumed: true, decision: answered });
    const one = [decision(row("q1"))];
    await render(rail(one));
    await click(marker());
    await click(button("Gosuggested"));
    dec.records = { q1: answered }; // what store.answer records; the overlay drops the row
    await render(rail(one));
    // Pending-only the moment the answer lands, while the held result stays readable.
    expect(section()!.textContent).toContain('Sent: "Go". Continuing…');
    expect([markerCount(), expectedCount()]).toEqual([null, 0]);
    await act(async () => panel()!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    await render(rail(one));
    expect(marker()).toBeNull(); // the hold released on close: zero hides the marker
  });

  // 409s (answered elsewhere / expired / withdrawn) read informative-or-neutral and leave the
  // count; a failed delivery (resumed: false) is the ONE warning — options kept, count kept.
  it.each([
    ["409 answered", answered, "Already answered", "1"],
    ["409 expired", row("q1", { state: "expired", expires_at: new Date().toISOString() }), "went ahead and carry on without this step", "1"],
    ["409 withdrawn", row("q1", { state: "withdrawn", withdraw_reason: "Found it." }), "doesn't need this any more. Found it.", "1"],
    ["resumed:false", null, "Couldn't send your answer", "2"],
  ] as const)("%s settles the card and keeps the count honest", async (_name, record, text, count) => {
    dec.answer.mockResolvedValue(record
      ? { answered_elsewhere: true, decision: record }
      : { answered_elsewhere: false, resumed: false, decision: row("q1") });
    await render(rail(items));
    await click(marker());
    await click(button("Gosuggested"));
    if (record) dec.records = { q1: record };
    await render(rail(items));
    expect(section()!.textContent).toContain(text);
    expect(!!section()!.querySelector(".pl-alert")).toBe(!record); // warning ONLY for the failed send
    if (!record) {
      expect(button("Try again")).toBeTruthy();
      expect(button("Gosuggested")!.disabled).toBe(false); // retry stays possible
    }
    expect([markerCount(), expectedCount()]).toEqual([count, Number(count)]);
  });

  it("source links ride the shared navigation; a failed open keeps the panel with a note", async () => {
    dec.openChat.mockResolvedValueOnce(false).mockResolvedValue(true);
    await render(rail(items));
    await click(marker());
    await click([...panel()!.querySelectorAll("a")].find((a) => a.textContent === "Open chat: scout"));
    expect(dec.openChat).toHaveBeenCalledWith("chat-q1", false);
    expect(panel()).toBeTruthy(); // failure: stays open, says so in place
    expect(panel()!.textContent).toContain("Couldn't open that chat");
    await click(button("Reply in chat instead"));
    expect(dec.openChat).toHaveBeenLastCalledWith("chat-q1", true);
    expect(panel()).toBeNull(); // success closes the panel
  });
});

it("stopped goal: Keep going re-arms; Drop confirms then clears; a failure says so in place", async () => {
  dec.rearm.mockResolvedValueOnce({ message: "ok" }).mockRejectedValue(new Error("down"));
  dec.clear.mockResolvedValue({ undo_token: "u" });
  await render(rail([stoppedGoal("g1")]));
  await click(marker());
  await click(button("Keep going"));
  expect(dec.rearm).toHaveBeenCalledWith("g1", {});
  expect(state.sources.goals.retry).toHaveBeenCalledTimes(1);
  await click(button("Drop the goal"));
  expect(dec.clear).not.toHaveBeenCalled(); // two-step: nothing dropped yet
  expect(section()!.textContent).toContain("Drop this goal?");
  await click(button("Drop the goal"));
  expect(dec.clear).toHaveBeenCalledWith("g1", false);
  expect(state.sources.goals.retry).toHaveBeenCalledTimes(2);
  await click(button("Keep going")); // the rejected re-arm: inline note, controls kept
  expect(section()!.textContent).toContain("That didn't go through");
  expect(button("Keep going")!.disabled).toBe(false);
});

