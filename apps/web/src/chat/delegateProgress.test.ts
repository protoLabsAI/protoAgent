// A coding delegate's live progress on its delegation card (#3979). Before this the `@`
// mention card was a spinner and a clock for the whole run. Pinned here: the wire decoder
// (bounded, defensive), the dispatcher routing a delegate-progress-v1 frame — live AND on
// a durable replay — the reducer landing it on the right card, and the render: plan
// checklist, current tool with its kind and file, recent tools, narration tail; visible
// while the card runs, folded into the card body once it settles.
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import { makeA2ADispatcher, type A2AFrame } from "../lib/api/a2aStream";
import { delegateProgressFromWire } from "../lib/delegateProgress";
import { delegationFromFrame } from "../lib/delegation";
import type { ChatMessage, DelegateProgressEvent, ToolCall } from "../lib/types";
import { DelegateProgressView } from "./DelegateProgressView";
import { ToolCalls } from "./ToolCalls";
import { applyDelegateProgress, planProgress, settleDelegateProgress, shortLocation, toolLine } from "./delegateProgress";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const MIME = "application/vnd.protolabs.delegate-progress-v1+json";
const CTX = "chat-1";

/** What graph/delegate_progress.py puts on the wire, mid-run. */
const wire = {
  id: "mention:claude-code",
  target: "claude-code",
  plan: [
    { content: "Read calc.py", status: "completed" },
    { content: "Add subtract()", status: "in_progress" },
    { content: "Run the tests", status: "pending" },
  ],
  current_tool: {
    id: "t2",
    name: "Edit",
    kind: "edit",
    status: "running",
    locations: [{ path: "/home/me/repo/src/calc.py", line: 4 }],
  },
  recent_tools: [
    { id: "t1", name: "Read calc.py", kind: "read", status: "completed", locations: [{ path: "/home/me/repo/src/calc.py", line: 1 }] },
    { id: "t2", name: "Edit", kind: "edit", status: "running", locations: [{ path: "/home/me/repo/src/calc.py", line: 4 }] },
  ],
  tool_count: 2,
  text: "I'll look at the existing `calc.py` first.\n\nHere's the file — adding subtract now.",
  done: false,
  ok: true,
};

function frame(data: unknown): A2AFrame {
  return {
    result: {
      statusUpdate: {
        taskId: "t1",
        contextId: CTX,
        status: { state: "TASK_STATE_WORKING", message: { parts: [{ data, metadata: { mimeType: MIME } } as never] } },
      },
    },
  } as A2AFrame;
}

describe("decoding a delegate-progress snapshot", () => {
  it("maps the snake_case wire onto the card's shape", () => {
    const evt = delegateProgressFromWire(wire)!;
    expect(evt.id).toBe("mention:claude-code");
    expect(evt.target).toBe("claude-code");
    expect(evt.plan).toHaveLength(3);
    expect(evt.currentTool).toEqual({
      id: "t2",
      name: "Edit",
      kind: "edit",
      status: "running",
      locations: [{ path: "/home/me/repo/src/calc.py", line: 4 }],
    });
    expect(evt.recentTools.map((t) => t.name)).toEqual(["Read calc.py", "Edit"]);
    expect(evt.toolCount).toBe(2);
    expect(evt.done).toBe(false);
  });

  it("rejects what is not a snapshot and re-caps what is", () => {
    expect(delegateProgressFromWire(null)).toBeNull();
    expect(delegateProgressFromWire({ target: "x" })).toBeNull(); // no card id
    expect(delegateProgressFromWire({ id: "c" })).toBeNull(); // no delegate
    const flood = delegateProgressFromWire({
      id: "c",
      target: "x",
      plan: Array.from({ length: 99 }, (_, i) => ({ content: `step ${i}`, status: "pending" })),
      recent_tools: Array.from({ length: 99 }, (_, i) => ({ name: `tool ${i}`, status: "completed" })),
      text: "y".repeat(5000),
      current_tool: { status: "running" }, // nameless → dropped, not rendered blank
    })!;
    expect(flood.plan!.length).toBeLessThanOrEqual(20);
    expect(flood.recentTools.length).toBeLessThanOrEqual(6);
    expect(flood.recentTools[flood.recentTools.length - 1].name).toBe("tool 98"); // the newest survive
    expect(flood.text!.length).toBeLessThanOrEqual(400);
    expect(flood.currentTool).toBeUndefined();
  });

  it("keeps a foreground ask's id, so its progress can find the row", () => {
    expect(delegationFromFrame({ id: "run-1", summary: "Add subtract" })).toEqual({ id: "run-1", summary: "Add subtract" });
  });
});

describe("the dispatcher routes progress frames", () => {
  it("on a live status update", () => {
    const onDelegateProgress = vi.fn();
    makeA2ADispatcher(CTX, { onDelegateProgress })(frame(wire));
    expect(onDelegateProgress).toHaveBeenCalledTimes(1);
    expect(onDelegateProgress.mock.calls[0][0].id).toBe("mention:claude-code");
  });

  it("on a durable replay (reload / reattach lands the run's last snapshot)", () => {
    const onDelegateProgress = vi.fn();
    makeA2ADispatcher(CTX, { onDelegateProgress })({
      result: {
        task: {
          id: "t1",
          contextId: CTX,
          status: { state: "TASK_STATE_COMPLETED" },
          history: [{ role: "ROLE_AGENT", parts: [{ data: { ...wire, done: true }, metadata: { mimeType: MIME } }] }],
        },
      },
    } as unknown as A2AFrame);
    expect(onDelegateProgress.mock.calls[0][0].done).toBe(true);
  });
});

describe("applyDelegateProgress", () => {
  const evt = delegateProgressFromWire(wire)!;
  const card = (id: string): ToolCall => ({ id, name: "@claude-code", status: "running" });

  it("lands on the NEWEST mention card with that id (an earlier turn's card shares it)", () => {
    const messages: ChatMessage[] = [
      { id: "a1", role: "assistant", content: "", toolCalls: [card("mention:claude-code")] },
      { id: "u2", role: "user", content: "@claude-code again" },
      { id: "a2", role: "assistant", content: "", toolCalls: [card("mention:claude-code")] },
    ];
    const next = applyDelegateProgress(messages, evt);
    expect(next[0]).toBe(messages[0]);
    expect(next[2].toolCalls![0].delegateProgress!["claude-code"].toolCount).toBe(2);
    expect(next[2].toolCalls![0].delegateProgress!["claude-code"]).not.toHaveProperty("id");
  });

  it("lands on a delegate_to ask row by its id, replacing (not merging) the last snapshot", () => {
    const ask: ChatMessage = {
      id: "ask",
      role: "assistant",
      content: "Add subtract to calc.py",
      addressedTo: "claude-code",
      delegation: { id: "run-1" },
    };
    const once = applyDelegateProgress([ask], { ...evt, id: "run-1" });
    const twice = applyDelegateProgress(once, { ...evt, id: "run-1", plan: undefined, toolCount: 5 });
    expect(twice[0].delegation!.progress!.toolCount).toBe(5);
    expect(twice[0].delegation!.progress!.plan).toBeUndefined();
  });

  it("returns the same array when no card matches", () => {
    const messages: ChatMessage[] = [{ id: "a", role: "assistant", content: "hi" }];
    expect(applyDelegateProgress(messages, { ...evt, id: "nope" })).toBe(messages);
  });

  it("a turn's end stops a never-finished delegation reading as live", () => {
    const ask: ChatMessage = {
      id: "ask",
      role: "assistant",
      content: "x",
      addressedTo: "claude-code",
      delegation: { id: "run-1", progress: { ...evt, done: false } },
    };
    const [settled] = settleDelegateProgress([ask]);
    expect(settled.delegation!.progress!.done).toBe(true);
    expect(settled.delegation!.progress!.ok).toBe(false);
    const finished = [settled];
    expect(settleDelegateProgress(finished)).toBe(finished);
  });
});

describe("labels", () => {
  it("shows the tail of a path and the line", () => {
    expect(shortLocation({ path: "/home/me/repo/src/calc.py", line: 4 })).toBe("src/calc.py:4");
    expect(shortLocation({ path: "calc.py" })).toBe("calc.py");
  });
  it("adds the file only when the title doesn't already name it", () => {
    expect(toolLine({ name: "Edit", status: "running", locations: [{ path: "/r/src/calc.py", line: 4 }] })).toBe(
      "Edit · src/calc.py:4",
    );
    expect(toolLine({ name: "Read calc.py", status: "completed", locations: [{ path: "/r/src/calc.py" }] })).toBe(
      "Read calc.py",
    );
  });
  it("counts plan progress", () => {
    expect(planProgress(delegateProgressFromWire(wire)!)).toBe("1/3");
  });
});

// ── render ────────────────────────────────────────────────────────────────────

let root: Root | null = null;
let host: HTMLElement | null = null;

async function mount(el: ReturnType<typeof createElement>): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(el);
  });
  return host;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

const text = (el: Element | null) => (el?.textContent ?? "").replace(/\s+/g, " ").trim();
/** A tool row as "<kind> <line>" — the two are separate spans. */
const toolText = (li: Element) =>
  [li.querySelector(".dp-kind"), li.querySelector(".dp-tool-name")].map(text).filter(Boolean).join(" ");

describe("DelegateProgressView", () => {
  const progress = (() => {
    const { id: _id, ...p } = delegateProgressFromWire(wire)! as DelegateProgressEvent;
    void _id;
    return p;
  })();

  it("renders the plan checklist, the current tool with kind + file, recent tools and the narration tail", async () => {
    const el = await mount(createElement(DelegateProgressView, { progress, live: true }));
    const plan = [...el.querySelectorAll(".dp-plan-entry")];
    expect(plan.map((li) => text(li))).toEqual(["Read calc.py", "Add subtract()", "Run the tests"]);
    expect(plan[0].className).toContain("dp-plan-entry--completed");
    expect(plan[1].className).toContain("dp-plan-entry--in_progress");
    expect(text(el.querySelector(".dp-head"))).toContain("plan 1/3");
    const current = el.querySelector(".dp-tool--current")!;
    expect(toolText(current)).toBe("edit Edit · src/calc.py:4");
    expect(current.getAttribute("title")).toBe("/home/me/repo/src/calc.py");
    // The current tool is not listed twice.
    expect([...el.querySelectorAll(".dp-tool")].map(toolText)).toEqual([
      "edit Edit · src/calc.py:4",
      "read Read calc.py",
    ]);
    expect(text(el.querySelector(".dp-text"))).toContain("adding subtract now");
    expect(el.querySelector(".delegate-progress--live")).not.toBeNull();
  });

  it("settled, it reads as the final state: no live tail, no spinner", async () => {
    const el = await mount(createElement(DelegateProgressView, { progress: { ...progress, done: true }, live: true }));
    expect(el.querySelector(".delegate-progress--live")).toBeNull();
    expect(el.querySelector(".dp-text")).toBeNull();
    expect(el.querySelector(".dp-tool--current")).toBeNull();
  });
});

describe("the @ mention card", () => {
  const evt = delegateProgressFromWire(wire)!;
  const withProgress = (status: ToolCall["status"]): ToolCall => {
    const [m] = applyDelegateProgress(
      [{ id: "a", role: "assistant", content: "", toolCalls: [{ id: "mention:claude-code", name: "@claude-code", status, startedAt: Date.now() }] }],
      evt,
    );
    return m.toolCalls![0];
  };

  it("shows the live view under the header while the delegate runs — no expanding needed", async () => {
    const el = await mount(createElement(ToolCalls, { calls: [withProgress("running")], streaming: true }));
    expect(el.querySelector(".delegate-progress--live")).not.toBeNull();
    expect(text(el.querySelector(".dp-tool--current"))).toContain("Edit");
  });

  it("folds it into the card body once the card settles", async () => {
    const el = await mount(createElement(ToolCalls, { calls: [withProgress("done")] }));
    // Collapsed by default: the final state is behind the card's own disclosure.
    expect(el.querySelector(".delegate-progress")).toBeNull();
    const toggle = el.querySelector("button[aria-expanded]") as HTMLButtonElement;
    await act(async () => toggle.click());
    expect(el.querySelector(".delegate-progress")).not.toBeNull();
    expect(el.querySelector(".delegate-progress--live")).toBeNull();
  });
});
