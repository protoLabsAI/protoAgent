import { afterEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { ToolCall } from "../lib/types";
import { ToolCalls } from "./ToolCalls";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(calls: ToolCall[], streaming = false): Promise<HTMLElement> {
  if (!host) {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  }
  await act(async () => {
    root!.render(createElement(ToolCalls, { calls, streaming }));
  });
  return host;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

const others: ToolCall[] = Array.from({ length: 4 }, (_, i) => ({
  id: `tool-${i}`, name: "calculator", status: "done", output: "42",
}));
const wait: ToolCall = {
  id: "wait", name: "wait", status: "done",
  input: JSON.stringify({ seconds: 600, then: `Finish the release. ${"x".repeat(900)}` }).slice(0, 800),
  output: "Wait scheduled: 10 minutes. Will resume to: Finish the release. …",
};

const foldText = (el: HTMLElement) => el.querySelector(".pl-toolcard-summary__head")?.textContent;

describe("collapsed wait summaries (#4852)", () => {
  it("exposes a cut-preview wait and its ETA outside the four-tool fold", async () => {
    const el = await render([...others, wait]);
    const summary = el.querySelector(".tool-wait-summary");
    expect(summary?.textContent).toContain("Waiting ~10 minutes · resumes with: Finish the release.");
    expect(summary?.closest(".pl-toolcard-summary")).toBeNull();
    expect(el.querySelector(".pl-toolcard__name")?.textContent).toContain("~10 minutes");
    expect(foldText(el)).toContain("4 tools");
    expect(foldText(el)).not.toContain("5 tools");
    expect(el.querySelector(".pl-toolcard__body")).toBeNull();
  });

  it.each([
    { output: "Error: scheduler unavailable" },
    { status: "error" as const },
    { stopped: true },
    { input: "garbage", output: "unknown" },
  ])("keeps unsuccessful or unresolved waits in the fold: %j", async (overrides) => {
    const el = await render([...others, { ...wait, ...overrides }]);
    expect(el.querySelector(".tool-wait-summary")).toBeNull();
    expect(foldText(el)).toContain("5 tools");
  });

  it("shows a lone settled wait in its lead slot", async () => {
    const el = await render([wait]);
    expect(el.querySelector(".tool-lead .tool-wait-summary")?.textContent).toContain("Waiting ~10 minutes");
    expect(el.querySelector(".pl-toolcard-summary")).toBeNull();
  });

  it("hides the summary while running and shows it when the call settles mid-stream", async () => {
    const el = await render([{ ...wait, status: "running", output: undefined }], true);
    expect(el.querySelector(".tool-wait-summary")).toBeNull();
    await render([wait], true);
    expect(el.querySelector(".tool-spotlight .tool-wait-summary")).not.toBeNull();
  });

  it("omits the resume clause for an empty plan and avoids an empty fold", async () => {
    const el = await render([wait, { ...wait, id: "wait-2", input: '{"seconds":60}' }]);
    const summaries = el.querySelectorAll(".tool-wait-summary");
    expect(summaries).toHaveLength(2);
    expect(summaries[1].textContent).toBe("Waiting ~1 minute");
    expect(el.querySelector(".pl-toolcard-summary")).toBeNull();
  });

  it("preserves the expanded fold when a streamed wait settles outside it", async () => {
    const el = await render([...others, { ...wait, status: "running" }], true);
    const head = el.querySelector<HTMLButtonElement>(".pl-toolcard-summary__head")!;
    await act(async () => head.click());
    expect(head.getAttribute("aria-expanded")).toBe("true");
    await render([...others, wait]);
    expect(el.querySelector(".pl-toolcard-summary__head")).toBe(head);
    expect(head.getAttribute("aria-expanded")).toBe("true");
    expect(foldText(el)).toContain("4 tools");
    expect(el.querySelector(".tool-wait-summary")).not.toBeNull();
  });
});
