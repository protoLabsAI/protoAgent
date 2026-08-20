// Render-level proof for #2896: background delegations — `delegate_to(background=true)` /
// `task(run_in_background=true)` — fold into ONE compact "N background jobs" chip instead
// of full-height ToolCards (even a single one), never take the streaming spotlight slot,
// and stay expandable to the individual dispatch cards. Foreground calls keep the existing
// spotlight/fold/settled behavior. Detection parses `call.input` at render time; a parse
// failure (args cut mid-stream) must read as foreground, never crash. (Same jsdom mount
// pattern as waitRender.test.ts.)
import { afterEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ToolCalls } from "./ToolCalls";
import type { ToolCall } from "../lib/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(props: {
  calls: ToolCall[];
  streaming?: boolean;
  spotlight?: boolean;
}): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(ToolCalls, props));
  });
  return host;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

let seq = 0;
function mkCall(over: Partial<ToolCall> & { name: string }): ToolCall {
  return { id: `tc-${++seq}`, status: "done", ...over };
}

const bgDelegate = (n: number) =>
  mkCall({
    name: "delegate_to",
    input: JSON.stringify({ agent: `worker-${n}`, task: "Dig in.", background: true }),
    output: `Started a background delegation (id: bg-${n}).`,
  });

const bgTask = () =>
  mkCall({
    name: "task",
    input: JSON.stringify({ description: "Deep dive", prompt: "Research.", run_in_background: true }),
    output: "Started a background task (id: bg-t1).",
  });

const fgSearch = () =>
  mkCall({
    name: "web_search",
    input: JSON.stringify({ query: "coding agents" }),
    output: "1 result(s) for 'coding agents':\n1. Example — https://example.com/x",
  });

function summaryTexts(el: HTMLElement): string[] {
  return [...el.querySelectorAll(".pl-toolcard-summary__text")].map((n) =>
    (n.textContent ?? "").replace(/\s+/g, " ").trim(),
  );
}

describe("background delegation chip (#2896)", () => {
  it("folds 3 settled background delegate_to calls into ONE chip, no full-height cards", async () => {
    const el = await render({ calls: [bgDelegate(1), bgDelegate(2), bgDelegate(3)] });
    expect(el.querySelectorAll(".tool-bg-summary").length).toBe(1);
    expect(summaryTexts(el)).toEqual(["3 background jobs"]);
    expect(el.querySelectorAll(".pl-toolcard").length).toBe(0);
  });

  it("folds even a SINGLE background delegation — no lone full-height card", async () => {
    const el = await render({ calls: [bgTask()] });
    expect(summaryTexts(el)).toEqual(["1 background job"]);
    expect(el.querySelectorAll(".pl-toolcard").length).toBe(0);
  });

  it("expands to the individual dispatch cards (DS summary disclosure)", async () => {
    const el = await render({ calls: [bgDelegate(1), bgDelegate(2), bgDelegate(3)] });
    const head = el.querySelector<HTMLElement>(".tool-bg-summary .pl-toolcard-summary__head");
    expect(head).toBeTruthy();
    await act(async () => head!.click());
    expect(el.querySelectorAll(".pl-toolcard").length).toBe(3);
    const names = [...el.querySelectorAll(".pl-toolcard__name")].map((n) => n.textContent);
    expect(names.every((n) => n?.includes("delegate_to"))).toBe(true);
  });

  it("mixed settled turn: the fg fan-out folds into its own chip, bg into the muted chip", async () => {
    const el = await render({ calls: [fgSearch(), fgSearch(), bgDelegate(1)] });
    const texts = summaryTexts(el);
    expect(texts).toContain("2 tools");
    expect(texts).toContain("1 background job");
    // Exactly one of the two chips is the muted background variant.
    expect(el.querySelectorAll(".pl-toolcard-summary").length).toBe(2);
    expect(el.querySelectorAll(".tool-bg-summary .pl-toolcard-summary").length).toBe(1);
  });

  it("mixed settled turn: a LONE fg tool still renders inline (fold behavior unchanged)", async () => {
    const el = await render({ calls: [fgSearch(), bgDelegate(1)] });
    const cards = [...el.querySelectorAll(".pl-toolcard")];
    expect(cards.length).toBe(1); // the web_search card — the bg dispatch stays folded
    expect(cards[0].querySelector(".pl-toolcard__name")?.textContent).toContain("web_search");
    expect(summaryTexts(el)).toEqual(["1 background job"]);
  });

  it("streaming: a background dispatch never takes the spotlight slot", async () => {
    const el = await render({
      calls: [
        fgSearch(),
        mkCall({
          name: "delegate_to",
          status: "running",
          input: JSON.stringify({ agent: "worker", task: "Dig in.", background: true }),
        }),
      ],
      streaming: true,
    });
    const slot = el.querySelector(".tool-spotlight");
    expect(slot).toBeTruthy();
    // The slot keeps the most-recent FOREGROUND tool, not the bg dispatch fired after it.
    expect(slot?.querySelector(".pl-toolcard__name")?.textContent).toContain("web_search");
    expect(summaryTexts(el)).toEqual(["1 background job"]);
  });

  it("streaming with ONLY background dispatches: no spotlight slot at all, just the chip", async () => {
    const el = await render({ calls: [bgDelegate(1), bgDelegate(2)], streaming: true });
    expect(el.querySelector(".tool-spotlight")).toBeNull();
    expect(summaryTexts(el)).toEqual(["2 background jobs"]);
    expect(el.querySelectorAll(".pl-toolcard").length).toBe(0);
  });

  it("spotlight mode (WorkBlock live slot): bg goes to the chip, not the slot", async () => {
    const el = await render({
      calls: [
        mkCall({
          name: "task",
          status: "running",
          input: JSON.stringify({ description: "Deep dive", run_in_background: true }),
        }),
      ],
      spotlight: true,
    });
    expect(el.querySelector(".tool-spotlight")).toBeNull();
    expect(summaryTexts(el)).toEqual(["1 background job"]);
  });

  it("guards: truncated args, background:false, and a plain task all stay foreground", async () => {
    const el = await render({
      calls: [
        // Args cut mid-stream — JSON.parse throws; must read as foreground, not crash.
        mkCall({ name: "delegate_to", input: '{"background": tru' }),
        mkCall({ name: "delegate_to", input: JSON.stringify({ background: false }), output: "ran inline" }),
        mkCall({ name: "task", input: JSON.stringify({ description: "sync" }), output: "done" }),
      ],
    });
    expect(el.querySelector(".tool-bg-summary")).toBeNull();
    expect(summaryTexts(el)).toEqual(["3 tools"]); // the normal settled fan-out fold
  });
});
