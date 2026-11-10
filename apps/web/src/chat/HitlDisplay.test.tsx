import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { HitlDisplayTable, HitlPayload } from "../lib/types";
import { HitlForm } from "./HitlForm";

import { decisionsApi, type DecisionRow } from "../lib/api/decisions";
import { ApiError } from "../lib/api/http";
import { ChatMessageView } from "./ChatMessageView";
import { DecisionTranscript, DecisionTranscriptContext, useLiveDecisionTranscript, hasDecisionTranscript } from "./HitlDisplay";
import { messagesFromDurableTurn } from "./sessionHydration";
import type { ChatMessage } from "../lib/types";
import { useHitl } from "./useHitl";
import slotSource from "./ChatSessionSlot.tsx?raw";


// Keep prose synchronous; the form and design-system controls/table are real.
vi.mock("./LazyMarkdown", () => ({ Markdown: ({ children }: { children: string }) => h("p", null, children) }));
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const hostile = '<img src=x onerror="alert(1)"><a href="https://example.com">Go</a><script>alert(1)</script>';
const display: HitlDisplayTable = {
  component: "table",
  props: { columns: ["Name", "Value"], rows: [[hostile, 0], [true, false], [null, "https://example.com"]] },
};
const approval: HitlPayload = { kind: "approval", description: "Review these values", detail: "echo <literal>", display };
let container: HTMLDivElement;
let root: Root;
let onSubmit: ReturnType<typeof vi.fn<(response: Record<string, unknown> | string) => void>>;
let onCancel: ReturnType<typeof vi.fn<() => void>>;
let onApproveAlways: ReturnType<typeof vi.fn<() => void>>;
const render = (payload = approval, busy = false) => act(() => root.render(h(HitlForm, {
  payload, busy, onSubmit, onCancel, onApproveAlways,
})));
const button = (label: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === label)!;
const click = (element: HTMLElement) => act(() => element.click());
const key = (element: Element, value: string) => act(() => element.dispatchEvent(new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true })));

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  onSubmit = vi.fn();
  onCancel = vi.fn();
  onApproveAlways = vi.fn();
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("HITL inert display", () => {
  it("renders semantic DS markup, scalar text, escaped hostile content, and retained prose", () => {
    render();
    expect(container.querySelector("table.pl-table")).not.toBeNull();
    expect([...container.querySelectorAll("th")].map((c) => c.textContent)).toEqual(["Name", "Value"]);
    expect([...container.querySelectorAll("td")].map((c) => c.textContent)).toEqual([hostile, "0", "true", "false", "", "https://example.com"]);
    expect(container.querySelector(".hitl-display img, .hitl-display a, .hitl-display script, iframe")).toBeNull();
    expect(container.textContent).toContain("Review these values");
    expect(container.querySelector("pre")?.textContent).toBe("echo <literal>");
  });

  it("display clicks and ordinary keys have no authority; Escape retains host dismissal", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    render();
    const table = container.querySelector("table")!;
    expect(table.querySelector("button, input, a, [tabindex], [contenteditable]")).toBeNull();
    for (const element of [table, ...table.querySelectorAll("th, td")]) {
      click(element as HTMLElement);
      for (const value of ["Enter", " ", "ArrowDown", "a"]) key(element, value);
    }
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
    expect(onApproveAlways).not.toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
    key(table, "Escape");
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("preserves exact host option values, focus, and busy disabling", () => {
    const options = [{ label: "Allow read", value: '  allow:read/一 "yes"  ', primary: true }, { label: "Reject", value: "reject_once" }];
    render({ ...approval, options });
    expect(document.activeElement).toBe(button("Allow read"));
    expect(container.querySelectorAll("button")).toHaveLength(2);
    options.forEach((option) => click(button(option.label)));
    expect(onSubmit.mock.calls).toEqual(options.map((option) => [option.value]));
    onSubmit.mockClear();
    render({ ...approval, options }, true);
    options.forEach((option) => {
      expect(button(option.label).disabled).toBe(true);
      click(button(option.label));
    });
    key(container.querySelector("table")!, "Escape");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("preserves legacy Approve/Deny/always, session restriction, busy and focus restoration", () => {
    const previous = document.createElement("button");
    document.body.appendChild(previous);
    previous.focus();
    render();
    expect(document.activeElement).toBe(button("Deny"));
    click(button("Approve"));
    click(button("Deny"));
    click(button("Approve & don't ask again"));
    expect(onSubmit.mock.calls).toEqual([["approved"], ["denied"]]);
    expect(onApproveAlways).toHaveBeenCalledTimes(1);
    render(approval, true);
    for (const b of container.querySelectorAll("button")) { expect(b.disabled).toBe(true); click(b); }
    expect(onSubmit).toHaveBeenCalledTimes(2);
    expect(onApproveAlways).toHaveBeenCalledTimes(1);
    render({ ...approval, session_allow: false });
    expect(button("Approve & don't ask again")).toBeUndefined();
    act(() => root.render(null));
    expect(document.activeElement).toBe(previous);
    previous.remove();
  });

  const payloads: HitlPayload[] = [
    approval,
    { kind: "form", description: "Form prose", steps: [{ schema: { properties: { name: { type: "string", default: "Ada" } } } }] },
    { question: "Question prose" },
  ];
  it.each(payloads)("renders display alongside every supported prompt: $kind $question", (payload) => {
    render({ ...payload, display });
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.textContent).toContain(payload.description || payload.question);
  });

  const invalid: unknown[] = [undefined, null, "table", [], {}, { component: "plugin:interactive", props: display.props },
    { component: "table", props: null }, { component: "table", props: { columns: "bad", rows: [] } },
    ...[
      { columns: [42], rows: [] }, { columns: ["x"], rows: "bad" }, { columns: ["x"], rows: [null] },
      { columns: ["x"], rows: [[]] }, { columns: ["x"], rows: [[{}]] }, { columns: ["x"], rows: [[[1]]] },
      { columns: ["x"], rows: [[NaN]] }, { columns: ["x"], rows: [[Infinity]] },
      { columns: ["x"], rows: [[undefined]] }, { columns: ["x".repeat(201)], rows: [] },
      { columns: ["x"], rows: [["x".repeat(1001)]] }, { columns: Array(17).fill("x"), rows: [] },
      { columns: ["x"], rows: Array(101).fill([1]) },
      { columns: ["x"], rows: Array(100).fill(["一".repeat(1000)]) },
    ].map((props) => ({ component: "table", props })),
  ];
  it.each(invalid.map((value, i) => [i, value] as const))("ignores invalid display %s and keeps approval/form/question usable", (_i, value) => {
    for (const payload of payloads) {
      act(() => root.render(null));
      render({ ...payload, display: value as HitlDisplayTable });
      expect(container.querySelector("table, iframe")).toBeNull();
      expect(container.textContent).toContain(payload.description || payload.question);
      if (payload.kind === "approval") {
        expect(container.querySelector("pre")?.textContent).toBe(approval.detail);
        click(button("Approve"));
        expect(onSubmit).toHaveBeenLastCalledWith("approved");
      } else if (payload.steps) {
        click(button("Submit"));
        expect(onSubmit).toHaveBeenLastCalledWith({ name: "Ada" });
      } else {
        expect(container.querySelector("textarea")).not.toBeNull();
        click(button("Dismiss"));
        expect(onCancel).toHaveBeenCalled();
      }
    }
  });

  it("accepts boundary dimensions and Unicode lengths while ignoring executable extra props", () => {
    const columns = Array(16).fill("😀".repeat(200));
    const rows = Array.from({ length: 100 }, () => Array(16).fill(null));
    rows[0][0] = "😀".repeat(1000);
    render({ ...approval, display: { component: "table", props: { columns, rows, onClick: onSubmit, dangerouslySetInnerHTML: { __html: hostile } } } as HitlDisplayTable });
    expect(container.querySelectorAll("td")).toHaveLength(1600);
    click(container.querySelector("td")!);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(container.querySelector("img")).toBeNull();
  });

  it("enforces the whole-table byte ceiling including JSON separator spaces", () => {
    const rows = Array.from({ length: 66 }, () => ["x".repeat(980)]);
    const candidate: HitlDisplayTable = { component: "table", props: { columns: ["x"], rows } };
    const size = new TextEncoder().encode(JSON.stringify(candidate)).length + 6 + rows.length - 1;
    rows.push(["x".repeat(65536 - size - 6)]); // an additional row adds six separator/quote bytes
    render({ ...approval, display: candidate });
    expect(container.querySelector("table")).not.toBeNull();
    rows[rows.length - 1][0] += "x";
    render({ ...approval, display: candidate });
    expect(container.querySelector("table")).toBeNull();
    expect(button("Approve").disabled).toBe(false);
  });
});

// Exercise the real shared cache, hook and card; only transport and bus are controlled.
const decisionBus = vi.hoisted(() => new Set<(payload: Record<string, unknown>) => void>());
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, callback: (payload: Record<string, unknown>) => void) => {
    if (topic === "decision.changed") decisionBus.add(callback);
    return () => decisionBus.delete(callback);
  },
  onConnectionChange: () => () => {},
}));
vi.mock("../lib/notify", () => ({ notifyIfHidden: vi.fn() }));


let sequence = 0;
const record = (): DecisionRow => ({
  id: `transcript-${++sequence}`, session_id: "chat-test", agent: "Engineer", question: "Ship?",
  options: [{ id: "ship", label: "Ship it" }], created_at: "2026-11-08T14:45:00Z",
  state: "pending", answer: null, answered_at: null, expires_at: null, default_option: null,
  thread_id: "thread", interrupt_id: "interrupt", context: "", default_action: null,
  withdraw_reason: null, answered_by: null, answered_surface: null,
  default_action_text: "carry on without this step", answer_label: null,
});
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("durable decision transcript", () => {
  let current: DecisionRow;
  beforeEach(() => {
    current = record();
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    vi.spyOn(decisionsApi, "listDecisions").mockImplementation(async () => ({
      decisions: current.state === "pending" ? [current] : [], pending: current.state === "pending" ? 1 : 0,
    }));
    vi.spyOn(decisionsApi, "getDecision").mockImplementation(async () => current);
  });
  afterEach(() => vi.unstubAllGlobals());
  const mount = async () => act(async () => {
    root.render(h(ChatMessageView, { message: {
      id: "assistant", role: "assistant", content: "", status: "streaming", paused: true, decisionId: current.id,
    } }));
    await flush();
  });

  it.each(["answered", "expired", "withdrawn", "cancelled"] as const)("reacts to pending → %s through the shared bus", async (state) => {
    await mount();
    expect(button("Ship it").disabled).toBe(false);
    expect(container.querySelectorAll('[role="group"][aria-label="Answer"]')).toHaveLength(1);
    current = { ...current, state, answer_label: "Ship it", answered_at: "2026-11-08T15:00:00Z" };
    await act(async () => { decisionBus.forEach((fn) => fn({ id: current.id })); await flush(); });
    expect(button("Ship it")).toBeUndefined();
    expect(container.textContent).toContain(state === "answered" ? 'You chose "Ship it"' : state === "expired" ? "No answer by" : "doesn't need this any more");
    expect(container.textContent).not.toContain("Waiting for your input");
  });

  const runTurn = vi.fn(async () => {});
  let liveHitl: ReturnType<typeof useHitl>;
  let bridge: ReturnType<typeof useLiveDecisionTranscript>;
  function LiveSlot() {
    liveHitl = useHitl({ sessionId: "chat-test", runTurn, noteToThread: vi.fn(), lastAssistantId: () => undefined });
    bridge = useLiveDecisionTranscript(liveHitl.hitlRef, liveHitl.updateHitl, liveHitl.resumeHitl);
    return h(DecisionTranscriptContext.Provider, { value: bridge },
      h("div", { className: "chat-session-slot" },
        h(DecisionTranscript, { id: current.id }),
        h("div", { className: "composer-wrap" }, h("textarea")),
      ));
  }
  async function mountLive() {
    runTurn.mockClear();
    await act(async () => { root.render(h(LiveSlot)); await flush(); });
    act(() => liveHitl.updateHitl({ kind: "decision", decision_id: current.id }));
  }

  it("arms a free-text interrupt answer from Reply in chat instead, once", async () => {
    await mountLive();
    expect(bridge.submitReply("ordinary steer")).toBe(false);
    click(button("Reply in chat instead"));
    expect(document.activeElement).toBe(container.querySelector("textarea"));
    act(() => { expect(bridge.submitReply("  Hold for review  ")).toBe(true); });
    expect(runTurn).toHaveBeenCalledExactlyOnceWith("Hold for review", { hitlResume: true });
    expect(liveHitl.hitlRef.current).toBeNull();
    expect(bridge.submitReply("next turn")).toBe(false);
    expect(slotSource).toContain("decisionTranscript.submitReply(draft)");
  });

  it.each(["answered", "expired", "withdrawn", "cancelled"] as const)(
    "clears the matching live interrupt on %s without a second resume",
    async (state) => {
      await mountLive();
      click(button("Reply in chat instead"));
      current = { ...current, state };
      await act(async () => { decisionBus.forEach((fn) => fn({ id: current.id })); await flush(); });
      expect(liveHitl.hitlRef.current).toBeNull();
      expect(bridge.submitReply("next turn")).toBe(false);
      expect(runTurn).not.toHaveBeenCalled();
    },
  );

  it("clears the live interrupt after an option answer, but retains it on delivery failure", async () => {
    const answer = vi.spyOn(decisionsApi, "answerDecision").mockImplementation(async () => ({
      answered_elsewhere: false, decision: current, resumed: false,
    }));
    await mountLive();
    await act(async () => { button("Ship it").click(); await flush(); });
    expect(liveHitl.hitlRef.current?.decision_id).toBe(current.id);
    answer.mockImplementation(async () => {
      current = { ...current, state: "answered" };
      return { answered_elsewhere: false, decision: current, resumed: true };
    });
    await act(async () => { button("Ship it").click(); await flush(); });
    expect(liveHitl.hitlRef.current).toBeNull();
    expect(runTurn).not.toHaveBeenCalled();
  });

  it("never arms or clears a different or generic live interrupt from a historical card", async () => {
    await mountLive();
    const other: HitlPayload = { kind: "decision", decision_id: "newer" };
    act(() => liveHitl.updateHitl(other));
    click(button("Reply in chat instead"));
    expect(bridge.submitReply("steer")).toBe(false);
    current = { ...current, state: "withdrawn" };
    await act(async () => { decisionBus.forEach((fn) => fn({ id: current.id })); await flush(); });
    expect(liveHitl.hitlRef.current).toBe(other);
    act(() => liveHitl.updateHitl(approval));
    act(() => bridge.settled(current.id));
    expect(liveHitl.hitlRef.current).toBe(approval);
    expect(runTurn).not.toHaveBeenCalled();
  });

  it("submits once through shared arbitration and uses a 409 terminal record", async () => {
    const answer = vi.spyOn(decisionsApi, "answerDecision").mockImplementation(async () => {
      current = { ...current, state: "expired" };
      return { answered_elsewhere: true, decision: current };
    });
    await mount();
    await act(async () => { button("Ship it").click(); await flush(); });
    expect(answer).toHaveBeenCalledExactlyOnceWith(current.id, { option_id: "ship", surface: "console" });
    expect(container.textContent).toContain("No answer by");
    expect(container.textContent).not.toContain("You chose");
    expect(button("Ship it")).toBeUndefined();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("rerenders without duplicate subscriptions, unmounts, and reads the next identity", async () => {
    await mount();
    await mount();
    expect(decisionBus.size).toBe(1);
    act(() => root.render(null));
    expect(decisionBus.size).toBe(0);
    current = { ...record(), question: "A different decision?" };
    await mount();
    expect(container.textContent).toContain("A different decision?");
    expect(container.querySelectorAll("article")).toHaveLength(1);
  });

  it("keeps a successful answer in the transcript and resets interaction state for a new ID", async () => {
    vi.spyOn(decisionsApi, "answerDecision").mockImplementation(async () => {
      current = { ...current, state: "answered", answer_label: "Ship it" };
      return { answered_elsewhere: false, decision: current, resumed: true };
    });
    await mount();
    await act(async () => { button("Ship it").click(); await flush(); });
    expect(container.textContent).toContain('You chose "Ship it"');
    await mount();
    expect(button("Ship it")).toBeUndefined();
    current = { ...record(), question: "Ship again?" };
    await mount();
    expect(button("Ship it").disabled).toBe(false);
    expect(container.textContent).not.toContain("You chose");
  });

  it("shows safe loading and missing-record text without generic answer controls", async () => {
    vi.mocked(decisionsApi.listDecisions).mockResolvedValue({ decisions: [], pending: 0 });
    let reject!: (reason: unknown) => void;
    vi.mocked(decisionsApi.getDecision).mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    await act(async () => { root.render(h(DecisionTranscript, { id: current.id })); });
    expect(container.textContent).toContain("Loading decision");
    await act(async () => { reject(new ApiError(404, "missing")); await flush(); });
    expect(container.textContent).toContain("no longer available");
    expect(container.querySelector("button, textarea")).toBeNull();
  });

  it("hydrates historical wire identity into the same terminal card without live-submit behavior", async () => {
    current = { ...current, state: "answered", answer_label: "Ship it" };
    const payload = { kind: "decision", decision_id: current.id, question: "Ship?" };
    const messages = messagesFromDurableTurn({
      task_id: "historic", state: "completed", last_updated: "2026-11-08T15:00:00Z", text: "Shipped.",
      status: { state: "completed" }, artifacts: [],
      history: [{ role: "ROLE_AGENT", parts: [{ data: payload, metadata: { mimeType: "application/vnd.protolabs.hitl-v1+json" } }] }],
    });
    const message = messages.find((m) => m.decisionId === current.id)!;
    expect(message).toBeDefined();
    await act(async () => { root.render(h(ChatMessageView, { message })); await flush(); });
    expect(container.textContent).toContain('You chose "Ship it"');
    expect(container.querySelector("button, textarea")).toBeNull();
  });

  it("only suppresses the generic form for its matching authoritative transcript", () => {
    const message: ChatMessage = { role: "assistant", content: "Ship?", status: "done", decisionId: current.id };
    const payload: HitlPayload = { kind: "decision", decision_id: current.id };
    expect(hasDecisionTranscript(payload, [message])).toBe(true);
    for (const fallback of [{ kind: "decision" }, { ...payload, decision_id: "" }, { ...payload, decision_id: "other" }, approval, { question: "Generic?", display }] satisfies HitlPayload[]) {
      expect(hasDecisionTranscript(fallback, [message])).toBe(false);
    }
    expect(hasDecisionTranscript(payload, [])).toBe(false);
    expect(slotSource).toContain("hasDecisionTranscript(hitl, messages)");
    expect(slotSource).toContain("hitl && !transcriptHandlesDecision && (");
    expect(slotSource).toContain("hitl && transcriptHandlesDecision && <HitlDisplay display={hitl.display} />");
    act(() => root.render(h(ChatMessageView, { message: { ...message, decisionId: undefined } })));
    expect(container.querySelector(".decision-transcript, button, textarea")).toBeNull();
    expect(decisionsApi.getDecision).not.toHaveBeenCalled();
  });
});
