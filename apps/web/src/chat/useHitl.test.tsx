// useHitl (#3862) — the pending HITL interrupt, extracted from ChatSessionSlot. Drives the
// hook through the same minimal createRoot/act renderHook as useAttachments.test.tsx (the
// console has no testing-library dep). Pins: state + ref mirror stay in step, the three
// answer paths (form/question, approval, plugin composer-form), dismiss, and that every
// handler is a per-render closure (a re-render with a new runTurn / lastAssistantId is
// what the next click uses).
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import type { HitlPayload } from "../lib/types";
import { useHitl, type HitlRunTurn, type UseHitlOptions } from "./useHitl";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type HookResult = ReturnType<typeof useHitl>;

let container: HTMLElement;
let root: Root;
let result: { current: HookResult };
let Probe: (props: UseHitlOptions) => null;

function renderHook(opts: UseHitlOptions) {
  result = { current: undefined as unknown as HookResult };
  Probe = function Probe(props: UseHitlOptions) {
    result.current = useHitl(props);
    return null;
  };
  act(() => root.render(h(Probe, opts)));
  return result;
}

// Re-render the SAME mounted Probe with new options (state and refs survive).
function rerender(opts: UseHitlOptions) {
  act(() => root.render(h(Probe, opts)));
}

async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
  });
}

const baseOpts = (over: Partial<UseHitlOptions> = {}): UseHitlOptions => ({
  sessionId: "sess-1",
  runTurn: mkRun(),
  noteToThread: vi.fn(),
  lastAssistantId: () => "asst-1",
  ...over,
});

const mkRun = () => vi.fn<HitlRunTurn>(async () => {});

const form: HitlPayload = { kind: "form", title: "Pick one", question: "Which?" };
const approval: HitlPayload = { kind: "approval", title: "Run it?", detail: "rm -rf build" };
const pluginForm: HitlPayload = { kind: "form", title: "Wizard", plugin_callback_id: "cb-1" };

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("useHitl — state + ref mirror", () => {
  it("starts with nothing pending", () => {
    const r = renderHook(baseOpts());
    expect(r.current.hitl).toBeNull();
    expect(r.current.hitlRef.current).toBeNull();
  });

  it("updateHitl sets the state AND the ref (async closures read the ref)", () => {
    const r = renderHook(baseOpts());
    act(() => r.current.updateHitl(form));
    expect(r.current.hitl).toBe(form);
    expect(r.current.hitlRef.current).toBe(form);
    act(() => r.current.updateHitl(null));
    expect(r.current.hitl).toBeNull();
    expect(r.current.hitlRef.current).toBeNull();
  });

  it("keeps the same ref object across renders", () => {
    const r = renderHook(baseOpts());
    const ref = r.current.hitlRef;
    act(() => r.current.updateHitl(form));
    expect(r.current.hitlRef).toBe(ref);
  });
});

describe("useHitl — resumeHitl", () => {
  it("answers a form as a VISIBLE hitlResume turn with the JSON response", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    act(() => r.current.updateHitl(form));
    await act(() => r.current.resumeHitl({ choice: "a" }));
    expect(runTurn).toHaveBeenCalledWith('{"choice":"a"}', { hitlResume: true });
    expect(r.current.hitl).toBeNull();
    expect(r.current.hitlRef.current).toBeNull();
  });

  it("sends a string answer verbatim", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    act(() => r.current.updateHitl(form));
    await act(() => r.current.resumeHitl("yes"));
    expect(runTurn).toHaveBeenCalledWith("yes", { hitlResume: true });
  });

  it("resumes an approval SILENTLY, continuing the paused assistant message", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    act(() => r.current.updateHitl(approval));
    await act(() => r.current.resumeHitl("approved"));
    expect(runTurn).toHaveBeenCalledWith("approved", { hidden: true, resumeMessageId: "asst-1", hitlResume: true });
    expect(r.current.hitl).toBeNull();
  });

  it("redeems a plugin composer-form through the plugin route, never runTurn", async () => {
    const runTurn = mkRun();
    const spy = vi.spyOn(api, "submitChatCommandForm").mockResolvedValue({ reply: "done!" } as Awaited<
      ReturnType<typeof api.submitChatCommandForm>
    >);
    const noteToThread = vi.fn();
    const r = renderHook(baseOpts({ runTurn, noteToThread }));
    act(() => r.current.updateHitl(pluginForm));
    await act(() => r.current.resumeHitl({ name: "x" }));
    expect(spy).toHaveBeenCalledWith({ callback_id: "cb-1", session_id: "sess-1", answers: { name: "x" } });
    expect(runTurn).not.toHaveBeenCalled();
    expect(noteToThread).toHaveBeenCalledWith("done!");
    expect(r.current.hitl).toBeNull();
  });

  it("sends a plugin form's string answer as empty answers, with an empty session id when none", async () => {
    const spy = vi.spyOn(api, "submitChatCommandForm").mockResolvedValue({} as Awaited<
      ReturnType<typeof api.submitChatCommandForm>
    >);
    const r = renderHook(baseOpts({ sessionId: null }));
    act(() => r.current.updateHitl(pluginForm));
    await act(() => r.current.resumeHitl("ignored"));
    expect(spy).toHaveBeenCalledWith({ callback_id: "cb-1", session_id: "", answers: {} });
  });

  it("re-opens the next wizard step a plugin returns, tagged with its callback id", async () => {
    vi.spyOn(api, "submitChatCommandForm").mockResolvedValue({
      form: { kind: "form", title: "Step 2" },
      callback_id: "cb-2",
    } as Awaited<ReturnType<typeof api.submitChatCommandForm>>);
    const r = renderHook(baseOpts());
    act(() => r.current.updateHitl(pluginForm));
    await act(() => r.current.resumeHitl({}));
    expect(r.current.hitl).toEqual({ kind: "form", title: "Step 2", plugin_callback_id: "cb-2" });
    expect(r.current.hitlRef.current).toEqual(r.current.hitl);
  });

  it("notes a plugin submit failure as a danger note", async () => {
    vi.spyOn(api, "submitChatCommandForm").mockRejectedValue(new Error("nope"));
    const noteToThread = vi.fn();
    const r = renderHook(baseOpts({ noteToThread }));
    act(() => r.current.updateHitl(pluginForm));
    await act(() => r.current.resumeHitl({}));
    await flush();
    expect(noteToThread).toHaveBeenCalledWith("⚠️ nope", { tone: "danger" });
  });
});

describe("useHitl — dismissHitl", () => {
  it("resumes with the dismissed sentinel, silently, on the paused message", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    act(() => r.current.updateHitl(form));
    await act(() => r.current.dismissHitl());
    expect(runTurn).toHaveBeenCalledTimes(1);
    const [text, opts] = runTurn.mock.calls[0];
    expect(text.startsWith("[dismissed] The operator dismissed this request")).toBe(true);
    expect(opts).toEqual({ hidden: true, resumeMessageId: "asst-1", hitlResume: true });
    expect(r.current.hitl).toBeNull();
  });

  it("just closes a plugin composer-form (no graph to resume)", async () => {
    const runTurn = mkRun();
    const spy = vi.spyOn(api, "submitChatCommandForm");
    const r = renderHook(baseOpts({ runTurn }));
    act(() => r.current.updateHitl(pluginForm));
    await act(() => r.current.dismissHitl());
    expect(runTurn).not.toHaveBeenCalled();
    expect(spy).not.toHaveBeenCalled();
    expect(r.current.hitl).toBeNull();
    expect(r.current.hitlRef.current).toBeNull();
  });
});

describe("useHitl — per-render closures", () => {
  it("a handler from a later render uses THAT render's runTurn and lastAssistantId", async () => {
    const first = mkRun();
    const second = mkRun();
    const r = renderHook(baseOpts({ runTurn: first, lastAssistantId: () => "asst-1" }));
    act(() => r.current.updateHitl(approval));
    rerender(baseOpts({ runTurn: second, lastAssistantId: () => "asst-2" }));
    await act(() => r.current.resumeHitl("approved"));
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith("approved", { hidden: true, resumeMessageId: "asst-2", hitlResume: true });
  });

  it("reads lastAssistantId when the handler runs, not when the hook rendered", async () => {
    const runTurn = mkRun();
    let last = "asst-early";
    const r = renderHook(baseOpts({ runTurn, lastAssistantId: () => last }));
    act(() => r.current.updateHitl(approval));
    last = "asst-late"; // the slot's memo is computed after the hook call on the same render
    await act(() => r.current.dismissHitl());
    expect(runTurn.mock.calls[0][1]).toMatchObject({ resumeMessageId: "asst-late" });
  });
});
