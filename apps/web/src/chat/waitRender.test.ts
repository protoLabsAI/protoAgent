// Render-level proof for #1914: mount the REAL ToolValue and assert what reaches the DOM —
// the waiting block for a successful `wait`, the plain fallback when the args preview is
// unreadable, the error renderer for a failed schedule, and untouched non-wait tools.
// (Same jsdom mount pattern as markdownMediaRender.test.ts.)
import { afterEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ToolValue } from "./tool-renderers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(props: {
  raw: string;
  role: "input" | "output";
  tool: string;
  input?: string;
}): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(ToolValue, props));
  });
  return host;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

const WAIT_OUTPUT = "Wait scheduled: 5 minutes. Will resume to: Check the deploy.";

describe("ToolValue waiting state for `wait` (#1914)", () => {
  it("renders the waiting block: duration, resume summary, keep-chatting hint", async () => {
    const el = await render({
      raw: WAIT_OUTPUT,
      role: "output",
      tool: "wait",
      input: '{"seconds": 300, "then": "Check the deploy. Then report back with the URL."}',
    });
    const block = el.querySelector(".tool-wait");
    expect(block).toBeTruthy();
    expect(block?.textContent).toContain("Waiting ~5 minutes");
    expect(block?.textContent).toContain("Check the deploy."); // first sentence only
    expect(block?.textContent).not.toContain("report back"); // …summarized, not dumped
    expect(block?.textContent).toContain("You can keep chatting");
    // The raw confirmation string is replaced, not repeated.
    expect(el.textContent).not.toContain("Wait scheduled:");
  });

  it("falls back to the plain success render when the args preview is unreadable", async () => {
    // e.g. the 800-char preview cut the JSON — never crash, never guess.
    const el = await render({
      raw: WAIT_OUTPUT,
      role: "output",
      tool: "wait",
      input: '{"seconds": 300, "then": "cut mid-strin',
    });
    expect(el.querySelector(".tool-wait")).toBeNull();
    expect(el.textContent).toContain("Wait scheduled: 5 minutes");
  });

  it("keeps a FAILED wait on the error renderer (Error: … wins over the waiting block)", async () => {
    const el = await render({
      raw: "Error: couldn't schedule the wake-up: scheduler is down",
      role: "output",
      tool: "wait",
      input: '{"seconds": 300, "then": "Check the deploy."}',
    });
    expect(el.querySelector(".tool-wait")).toBeNull();
    expect(el.querySelector(".tool-error")).toBeTruthy();
  });

  it("leaves non-wait tools untouched even when an input is passed along", async () => {
    const el = await render({
      raw: "6 * 7 = 42",
      role: "output",
      tool: "calculator",
      input: '{"expression": "6 * 7"}',
    });
    expect(el.querySelector(".tool-wait")).toBeNull();
    expect(el.querySelector(".tool-calc")).toBeTruthy();
  });

  it("renders the wait INPUT section as plain args (only the output gets the block)", async () => {
    const el = await render({
      raw: '{"seconds": 300, "then": "Check the deploy."}',
      role: "input",
      tool: "wait",
    });
    expect(el.querySelector(".tool-wait")).toBeNull();
    expect(el.querySelector(".tool-kv")).toBeTruthy(); // generic key/value grid
  });
});
