// Render-level proof for the DS ds-awm adoption (@protolabsai/ui ≥0.74.1): a fold chip that
// holds a failed call colours ONLY its "· N failed" suffix red — a nested
// `.pl-toolcard-summary__failed` span — while the "N tools" count keeps the head's muted
// colour, so one failure among many no longer reads as the whole block failing. The label's
// textContent is unchanged. (Same jsdom mount pattern as waitSummaryRender.test.ts.)
import { afterEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { ToolCall } from "../lib/types";
import { ToolCalls } from "./ToolCalls";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(calls: ToolCall[]): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(ToolCalls, { calls, streaming: false }));
  });
  return host;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

const call = (i: number, over: Partial<ToolCall> = {}): ToolCall => ({
  id: `tool-${i}`, name: "calculator", status: "done", output: "42", ...over,
});
const failed = (i: number, over: Partial<ToolCall> = {}): ToolCall =>
  call(i, { status: "error", output: "Error: division by zero", ...over });
const background = { name: "delegate_to", input: JSON.stringify({ agent: "worker", task: "Dig in.", background: true }) };

const text = (el: Element | null) => (el?.textContent ?? "").replace(/\s+/g, " ").trim();

describe("fold chip failed suffix (ds-awm)", () => {
  it("wraps only the '· N failed' suffix in the failed span; the count stays outside it", async () => {
    const el = await render([call(0), call(1), failed(2), call(3), call(4)]);
    const label = el.querySelector(".pl-toolcard-summary__text");
    expect(text(label)).toBe("5 tools · 1 failed");
    const suffixes = label!.querySelectorAll(".pl-toolcard-summary__failed");
    expect(suffixes).toHaveLength(1);
    expect(text(suffixes[0])).toBe("· 1 failed");
    // The count is the label's own text, not part of the red span.
    const outside = [...label!.childNodes].filter((n) => n !== suffixes[0]).map((n) => n.textContent).join("");
    expect(outside.trim()).toBe("5 tools");
    // The red ✕ status glyph stays on the chip head.
    expect(el.querySelector(".pl-toolcard-summary__head > .pl-toolcard__status--error")).not.toBeNull();
  });

  it("counts every failed call in the suffix", async () => {
    const el = await render([failed(0), call(1), failed(2), call(3)]);
    expect(text(el.querySelector(".pl-toolcard-summary__text"))).toBe("4 tools · 2 failed");
    expect(text(el.querySelector(".pl-toolcard-summary__failed"))).toBe("· 2 failed");
  });

  it("renders no failed span when nothing in the fold failed", async () => {
    const el = await render([call(0), call(1), call(2), call(3)]);
    expect(text(el.querySelector(".pl-toolcard-summary__text"))).toBe("4 tools");
    expect(el.querySelector(".pl-toolcard-summary__failed")).toBeNull();
  });

  it("gives the background-jobs chip the same suffix span", async () => {
    const el = await render([call(0, background), failed(1, background)]);
    const label = el.querySelector(".tool-bg-summary .pl-toolcard-summary__text");
    expect(text(label)).toBe("2 background jobs · 1 failed");
    expect(text(label!.querySelector(".pl-toolcard-summary__failed"))).toBe("· 1 failed");
  });
});
