import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DecisionCard } from "./DecisionCard";
import { DecisionsPanel, DecisionsSheet } from "./DecisionsPanel";
import { createDecisionsStore } from "./useDecisions";
import { decisionsApi, type DecisionRow } from "../lib/api/decisions";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The decision bus + the native-notification seam, mocked so the store is driven by hand.
const bus = vi.hoisted(() => ({
  listeners: new Set<(d: Record<string, unknown>) => void>(),
  connections: new Set<(c: boolean) => void>(),
  topics: [] as string[],
}));
const mocks = vi.hoisted(() => ({ notify: vi.fn() }));
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (d: Record<string, unknown>) => void) => {
    bus.topics.push(topic);
    bus.listeners.add(fn);
    return () => bus.listeners.delete(fn);
  },
  onConnectionChange: (fn: (c: boolean) => void) => {
    bus.connections.add(fn);
    fn(true); // the store swallows the first (initial) connected callback
    return () => bus.connections.delete(fn);
  },
}));
vi.mock("../lib/notify", () => ({ notifyIfHidden: mocks.notify }));

const decision = (id: string, patch: Partial<DecisionRow> = {}): DecisionRow => ({
  id,
  session_id: "s1",
  agent: "protoEngineer",
  question: "Merge the release?",
  options: [
    { id: "merge", label: "Merge with the WARN", description: "I'll merge and cut the release.", recommended: true },
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
const listOf = (decisions: DecisionRow[]) => ({ decisions, pending: decisions.length });
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

let cleanup: (() => void)[] = [];
let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.restoreAllMocks();
  bus.listeners.clear();
  bus.connections.clear();
  bus.topics.length = 0;
  mocks.notify.mockClear();
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  cleanup = [];
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  cleanup.forEach((off) => off());
  await act(async () => root.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderNode(node: ReactNode) {
  await act(async () => {
    root.render(node);
  });
}
const buttons = () => [...document.querySelectorAll<HTMLButtonElement>("button")];
async function click(label: string) {
  const btn = buttons().find((b) => b.textContent?.includes(label) && !b.disabled);
  if (!btn) throw new Error(`no enabled button matching ${label}`);
  await act(async () => {
    btn.click();
  });
  await act(async () => {
    await Promise.resolve();
  });
}

function mountStore() {
  const store = createDecisionsStore();
  cleanup.push(store.subscribe(() => {}));
  return store;
}

// ── useDecisions store ────────────────────────────────────────────────────────────────────
describe("useDecisions store", () => {
  it("keeps the server order, exposes top/pendingCount, hidden until first load", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("a"), decision("b")]));
    const store = mountStore();
    expect(store.getSnapshot().loaded).toBe(false);
    expect(store.getSnapshot().state).toBe("loading");
    await settle();
    expect(store.getSnapshot().decisions.map((d) => d.id)).toEqual(["a", "b"]);
    expect(store.getSnapshot().pendingCount).toBe(2);
    expect(store.getSnapshot().top?.id).toBe("a");
    expect(store.getSnapshot().loaded).toBe(true);
    expect(store.getSnapshot().state).toBe("ready");
  });

  it("refreshes on decision.changed and on reconnect, and never polls", async () => {
    const list = vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    mountStore();
    await settle();
    expect(list).toHaveBeenCalledTimes(1);
    expect(bus.topics).toContain("decision.changed");

    bus.listeners.forEach((fn) => fn({ id: "a", session_id: "s1", state: "pending" }));
    await settle();
    expect(list).toHaveBeenCalledTimes(2);

    // A drop (disconnect) never refreshes; a reconnect does.
    bus.connections.forEach((fn) => fn(false));
    expect(list).toHaveBeenCalledTimes(2);
    bus.connections.forEach((fn) => fn(true));
    await settle();
    expect(list).toHaveBeenCalledTimes(3);
  });

  it("notifies once per new pending id, with a click that opens the panel", async () => {
    const list = vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    const store = mountStore();
    await settle();
    expect(mocks.notify).not.toHaveBeenCalled();

    list.mockResolvedValue(listOf([decision("dec-1")]));
    bus.listeners.forEach((fn) => fn({ id: "dec-1", session_id: "s1", state: "pending" }));
    await settle();
    expect(mocks.notify).toHaveBeenCalledTimes(1);
    const [title, body, onClick] = mocks.notify.mock.calls[0];
    expect(title).toBe("protoEngineer needs a decision");
    expect(body).toBe("Merge the release?");
    expect(onClick).toBe(store.openPanel);

    expect(store.getSnapshot().panelOpen).toBe(false);
    (onClick as () => void)();
    expect(store.getSnapshot().panelOpen).toBe(true);

    // A reload with the same id does not notify again (§4.5).
    bus.listeners.forEach((fn) => fn({ id: "dec-1", session_id: "s1", state: "pending" }));
    await settle();
    expect(mocks.notify).toHaveBeenCalledTimes(1);
  });

  it("opens and closes the panel from outside", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    const store = mountStore();
    await settle();
    expect(store.getSnapshot().panelOpen).toBe(false);
    store.openPanel();
    expect(store.getSnapshot().panelOpen).toBe(true);
    store.closePanel();
    expect(store.getSnapshot().panelOpen).toBe(false);
  });

  it("answers via the API with the console surface, and returns a 409 as answered-elsewhere", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("a")]));
    const answer = vi.spyOn(decisionsApi, "answerDecision");
    const store = mountStore();
    await settle();

    answer.mockResolvedValue({ answered_elsewhere: false, decision: decision("a", { state: "answered" }), resumed: true });
    const ok = await store.answer("a", "merge");
    expect(answer).toHaveBeenCalledWith("a", { option_id: "merge", surface: "console" });
    expect(ok).toMatchObject({ answered_elsewhere: false, resumed: true });

    answer.mockResolvedValue({ answered_elsewhere: true, decision: decision("a", { state: "answered" }) });
    const conflict = await store.answer("a", "merge");
    expect(conflict.answered_elsewhere).toBe(true);
  });
});

// ── DecisionCard ──────────────────────────────────────────────────────────────────────────
describe("DecisionCard", () => {
  it("renders a pending decision: md options, recommended primary + suggested, never danger", async () => {
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} />);
    const merge = buttons().find((b) => b.textContent?.includes("Merge with the WARN"));
    expect(merge?.className).toContain("pl-btn--primary");
    const hold = buttons().find((b) => b.textContent?.trim() === "Hold the release");
    expect(hold?.className).not.toContain("pl-btn--primary");
    expect(buttons().some((b) => b.className.includes("pl-btn--danger"))).toBe(false);
    expect(document.body.textContent).toContain("suggested");
    expect(document.body.textContent).toContain("I'll merge and cut the release.");
    expect(document.body.textContent).toContain("Reply in chat instead");
  });

  it("answering calls onAnswer and shows the Sent line; the panel holds the card", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: false, decision: decision("a"), resumed: true });
    const onHold = vi.fn();
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} onHold={onHold} />);
    await click("Merge with the WARN");
    expect(onAnswer).toHaveBeenCalledWith("merge");
    expect(onHold).toHaveBeenCalled();
    expect(document.body.textContent).toContain('Sent: "Merge with the WARN"');
    expect(document.body.textContent).toContain("picking it back up");
  });

  it("shows 'answered elsewhere' (info callout) on a 409 result", async () => {
    const onAnswer = vi.fn().mockResolvedValue({
      answered_elsewhere: true,
      decision: decision("a", { state: "answered", answer_label: "Hold the release", answered_at: "2026-11-08T14:52:00+00:00" }),
    });
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} />);
    await click("Merge with the WARN");
    expect(document.body.textContent).toContain("Already answered");
    expect(document.body.textContent).toContain("Hold the release");
    expect(document.querySelector(".pl-callout--info")).toBeTruthy();
  });

  it("keeps the options enabled on a send failure and offers Try again", async () => {
    const onAnswer = vi.fn().mockRejectedValue(new Error("network"));
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} />);
    await click("Merge with the WARN");
    expect(document.body.textContent).toContain("Couldn't send your answer");
    expect(document.querySelector(".pl-alert--warning")).toBeTruthy();
    expect(buttons().some((b) => b.textContent?.trim() === "Try again")).toBe(true);
    expect(buttons().find((b) => b.textContent?.includes("Merge with the WARN"))?.disabled).toBe(false);
    expect(buttons().find((b) => b.textContent?.trim() === "Hold the release")?.disabled).toBe(false);
  });

  it("treats a 200 with resumed=false as a failed send, not a Sent success", async () => {
    // Delivery to the parked turn failed; the decision is still pending server-side. The card
    // must NOT show "Sent" — it keeps the options enabled and offers Try again (§4.4).
    const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: false, decision: decision("a"), resumed: false });
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} />);
    await click("Merge with the WARN");
    expect(document.body.textContent).not.toContain("Sent:");
    expect(document.body.textContent).toContain("Couldn't send your answer");
    expect(document.querySelector(".pl-alert--warning")).toBeTruthy();
    expect(buttons().some((b) => b.textContent?.trim() === "Try again")).toBe(true);
    expect(buttons().find((b) => b.textContent?.includes("Merge with the WARN"))?.disabled).toBe(false);
    expect(buttons().find((b) => b.textContent?.trim() === "Hold the release")?.disabled).toBe(false);
  });

  it("renders the expired state as a neutral callout with the default action", async () => {
    await renderNode(
      <DecisionCard
        decision={decision("a", {
          state: "expired",
          expires_at: "2026-11-08T15:45:00+00:00",
          default_option: "hold",
          default_action: "keep holding the release",
          default_action_text: "keep holding the release",
        })}
        onAnswer={vi.fn()}
      />,
    );
    expect(document.body.textContent).toContain("went ahead and keep holding the release");
    expect(document.querySelector(".pl-callout")).toBeTruthy();
    expect(document.querySelector(".pl-callout--info")).toBeFalsy();
    expect(buttons().some((b) => b.textContent?.includes("Merge with the WARN"))).toBe(false);
  });

  it("renders the no-longer-needed state with the agent's reason", async () => {
    await renderNode(
      <DecisionCard decision={decision("a", { state: "withdrawn", withdraw_reason: "I merged it myself." })} onAnswer={vi.fn()} />,
    );
    expect(document.body.textContent).toContain("protoEngineer doesn't need this any more.");
    expect(document.body.textContent).toContain("I merged it myself.");
    expect(document.querySelector(".pl-callout--info")).toBeFalsy();
  });

  it("agent offline, backend holds: neutral dot, line, options stay enabled", async () => {
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} agentOnline={false} />);
    expect(document.body.textContent).toContain("protoEngineer is offline. It gets your answer when it starts again.");
    expect(document.querySelector(".pl-dot")).toBeTruthy();
    expect(buttons().find((b) => b.textContent?.includes("Merge with the WARN"))?.disabled).toBe(false);
  });

  it("agent offline, no hold: options disabled, Start action", async () => {
    const onStart = vi.fn();
    await renderNode(
      <DecisionCard decision={decision("a")} onAnswer={vi.fn()} agentOnline={false} agentHoldsOffline={false} onStartAgent={onStart} />,
    );
    expect(document.body.textContent).toContain("protoEngineer is offline. Start it to answer.");
    expect(buttons().find((b) => b.textContent?.includes("Merge with the WARN"))?.disabled).toBe(true);
    await act(async () => {
      buttons().find((b) => b.textContent?.trim() === "Start")!.click();
    });
    expect(onStart).toHaveBeenCalled();
  });

  it("shows the fallback-action line only with an expiry, and the Open chat link", async () => {
    const onOpenChat = vi.fn();
    await renderNode(
      <DecisionCard
        decision={decision("a", { expires_at: "2026-11-08T15:45:00+00:00", default_option: "hold", default_action_text: "keep holding the release" })}
        onAnswer={vi.fn()}
        onOpenChat={onOpenChat}
        chatTitle="Cut v0.227.0"
      />,
    );
    expect(document.body.textContent).toContain("If nobody answers by");
    expect(document.body.textContent).toContain("protoEngineer will keep holding the release");
    const link = [...document.querySelectorAll("a")].find((a) => a.textContent?.includes("Open chat"));
    expect(link?.textContent).toContain("Cut v0.227.0");
    await act(async () => link!.click());
    expect(onOpenChat).toHaveBeenCalled();
  });

  it("hides the fallback-action line when there is no expiry", async () => {
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} />);
    expect(document.body.textContent).not.toContain("If nobody answers");
  });
});

// ── DecisionsPanel / DecisionsSheet ─────────────────────────────────────────────────────────
describe("DecisionsPanel", () => {
  it("shows two skeleton blocks on first load", async () => {
    await renderNode(<DecisionsPanel decisions={[]} loading onAnswer={vi.fn()} />);
    expect(document.querySelector("[data-testid='decisions-loading']")).toBeTruthy();
    expect(document.querySelectorAll(".pl-skel-lines").length).toBe(2);
  });

  it("shows the nothing-waiting line when empty from the start", async () => {
    await renderNode(<DecisionsPanel decisions={[]} loading={false} onAnswer={vi.fn()} />);
    expect(document.body.textContent).toContain("Nothing is waiting on you. When an agent stops");
    expect(document.body.textContent).not.toContain("That's everything");
  });

  it("shows the That's-everything line after the last decision clears", async () => {
    await renderNode(<DecisionsPanel decisions={[decision("a")]} loading={false} onAnswer={vi.fn()} />);
    expect(document.body.textContent).toContain("Your agents stopped to ask you these.");
    await renderNode(<DecisionsPanel decisions={[]} loading={false} onAnswer={vi.fn()} />);
    expect(document.body.textContent).toContain("That's everything. Nothing is waiting on you.");
  });

  it("lists cards in order with a divider between them and no Popover", async () => {
    await renderNode(
      <DecisionsPanel
        decisions={[decision("a", { agent: "protoEngineer" }), decision("b", { agent: "Cindi" })]}
        loading={false}
        onAnswer={vi.fn()}
      />,
    );
    expect(document.querySelectorAll("article").length).toBe(2);
    expect(document.querySelectorAll(".pl-divider").length).toBe(1);
    expect(document.querySelector(".pl-popover")).toBeFalsy();
    expect(document.body.textContent).toContain("Cindi");
  });

  it("DecisionsSheet wraps the panel in a bottom Drawer titled 'Decisions waiting on you'", async () => {
    await renderNode(<DecisionsSheet open onClose={vi.fn()} decisions={[decision("a")]} loading={false} onAnswer={vi.fn()} />);
    expect(document.querySelector(".pl-drawer--bottom")).toBeTruthy();
    expect(document.body.textContent).toContain("Decisions waiting on you");
    expect(document.body.textContent).toContain("Your agents stopped to ask you these.");
  });
});

// ── decisions HTTP client ───────────────────────────────────────────────────────────────────
describe("decisions HTTP client", () => {
  it("lists pending decisions", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(listOf([decision("a")]))));
    vi.stubGlobal("fetch", fetch);
    const res = await decisionsApi.listDecisions("pending");
    expect(res.decisions[0].id).toBe("a");
    expect(String(fetch.mock.calls[0][0])).toContain("/api/decisions?state=pending");
  });

  it("answers via POST with { option_id, surface: console }", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ decision: decision("a", { state: "answered" }), resumed: true })));
    vi.stubGlobal("fetch", fetch);
    const res = await decisionsApi.answerDecision("a", { option_id: "merge" });
    expect(res).toMatchObject({ answered_elsewhere: false, resumed: true });
    const [url, init] = fetch.mock.calls[0];
    expect(String(url)).toContain("/api/decisions/a/answer");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ option_id: "merge", surface: "console" });
  });

  it("resolves a 409 as answered-elsewhere, carrying the closed record", async () => {
    const closed = decision("a", { state: "answered", answer_label: "Merge with the WARN" });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: closed }), { status: 409 }));
    vi.stubGlobal("fetch", fetch);
    const res = await decisionsApi.answerDecision("a", { option_id: "merge" });
    expect(res.answered_elsewhere).toBe(true);
    expect(res.decision?.answer_label).toBe("Merge with the WARN");
  });

  it("throws an ApiError on a non-409 failure", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "boom" }), { status: 500 }));
    vi.stubGlobal("fetch", fetch);
    await expect(decisionsApi.answerDecision("a", { option_id: "merge" })).rejects.toMatchObject({ name: "ApiError", status: 500 });
  });

  it("cancels via POST with a reason", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ decision: decision("a", { state: "cancelled" }) })));
    vi.stubGlobal("fetch", fetch);
    const res = await decisionsApi.cancelDecision("a", "dismissed");
    expect(res.decision.state).toBe("cancelled");
    const [url, init] = fetch.mock.calls[0];
    expect(String(url)).toContain("/api/decisions/a/cancel");
    expect(JSON.parse(init.body as string)).toEqual({ reason: "dismissed" });
  });
});
