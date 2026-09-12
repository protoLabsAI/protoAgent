// A single-tool turn's card must SURVIVE the streaming→settled transition, not be rebuilt by it.
//
// While a turn is live, `ToolCalls` holds the current foreground tool in the spotlight slot;
// once it settles, a lone tool renders inline (no pointless "1 tool" chip). Those used to be
// two different trees — `div.tool-spotlight > ToolGroup[key="__spotlight__"]` live, a bare
// `ToolGroup[key=call.id]` settled — so React tore the card down and built a new one at the
// exact moment the turn finished. The DS `ToolCard`'s `open` is uncontrolled
// (`useState(defaultOpen)`), so the rebuild silently collapsed whatever the operator had
// expanded mid-turn. Same class as the summary chips in #3390, the one case that fix left.
//
// Node identity is the honest assertion: same DOM node across the transition ⇒ React updated
// it in place ⇒ its disclosure state is intact. "Is it still open?" alone would pass on a
// remount that happened to default open. (Same jsdom mount pattern as
// backgroundChipRender.test.ts.)
import { afterEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ToolCalls } from "./ToolCalls";
import type { ToolCall } from "../lib/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

type ToolCallsProps = { calls: ToolCall[]; streaming?: boolean };

async function render(props: ToolCallsProps): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(ToolCalls, props));
  });
  return host;
}

/** Re-render the SAME root with new props — the transition as React actually performs it,
 *  which is the only way to observe a remount. */
async function rerender(props: ToolCallsProps): Promise<void> {
  await act(async () => {
    root!.render(createElement(ToolCalls, props));
  });
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

const INPUT = JSON.stringify({ query: "coding agents" });
const OUTPUT = "1 result(s) for 'coding agents':\n1. Example — https://example.com/x";

// The same call as the wire delivers it: RUNNING at the start frame, DONE (with its result)
// after the end frame. Same id both times — that's the call's identity across the settle.
const running = (id = "tc-lone"): ToolCall => ({ id, name: "web_search", status: "running", input: INPUT });
const done = (id = "tc-lone"): ToolCall => ({ id, name: "web_search", status: "done", input: INPUT, output: OUTPUT, durationMs: 820 });

const bgDispatch = (): ToolCall => ({
  id: "tc-bg",
  name: "delegate_to",
  status: "done",
  input: JSON.stringify({ agent: "worker", task: "Dig in.", background: true }),
  output: "Started a background delegation (id: bg-1).",
});

const card = (el: HTMLElement) => el.querySelector<HTMLElement>(".pl-toolcard");
const head = (el: HTMLElement) => el.querySelector<HTMLButtonElement>(".pl-toolcard__head");

describe("a single-tool turn's card survives the settle", () => {
  it("keeps the lone card's DOM node from the live spotlight to the settled inline card", async () => {
    const el = await render({ calls: [running()], streaming: true });
    const live = card(el);
    expect(live).not.toBeNull();
    expect(el.querySelector(".tool-spotlight")).not.toBeNull(); // really the live spotlight slot

    await rerender({ calls: [done()], streaming: false });
    expect(card(el)).toBe(live);
  });

  it("keeps an operator's mid-turn expansion (the reason the node matters)", async () => {
    const el = await render({ calls: [running()], streaming: true });
    await act(async () => head(el)!.click());
    expect(head(el)!.getAttribute("aria-expanded")).toBe("true");

    await rerender({ calls: [done()], streaming: false });
    expect(head(el)!.getAttribute("aria-expanded")).toBe("true");
    // …and the body now carries the result that arrived with the settle.
    expect(el.querySelector(".pl-toolcard__body")?.textContent).toContain("coding agents");
  });

  it("holds when a background chip rides beside the lone foreground card", async () => {
    // A mixed turn: the bg dispatch folds into its own chip, the fg tool is still the lone
    // card — a different child count on each side of the settle, same invariant.
    const el = await render({ calls: [running(), bgDispatch()], streaming: true });
    const live = card(el);
    const liveChip = el.querySelector(".tool-bg-summary");
    expect(live).not.toBeNull();

    await rerender({ calls: [done(), bgDispatch()], streaming: false });
    expect(card(el)).toBe(live);
    expect(el.querySelector(".tool-bg-summary")).toBe(liveChip); // #3390's guard still holds
  });

  it("leaves the settled lone card OUT of the spotlight marker", async () => {
    // `.tool-spotlight` is the live-only marker e2e/toolcard.ts gates on ("zero ⇒ the turn has
    // settled"); keeping the node must not keep the marker.
    const el = await render({ calls: [running()], streaming: true });
    await rerender({ calls: [done()], streaming: false });
    expect(el.querySelector(".tool-spotlight")).toBeNull();
    expect(el.querySelectorAll(".pl-toolcard")).toHaveLength(1);
  });

  it("a history-loaded lone card (never live) still renders exactly one inline card", async () => {
    const el = await render({ calls: [done()] });
    expect(el.querySelectorAll(".pl-toolcard")).toHaveLength(1);
    expect(el.querySelector(".tool-spotlight")).toBeNull();
    expect(el.querySelector(".pl-toolcard-summary")).toBeNull(); // no "1 tool" chip
  });
});
