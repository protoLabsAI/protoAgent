// WorkBlock streamed-artifact spotlight (ADR 0118 D3 / S8c). createRoot/act + hand-driven jsdom,
// like the other console UI suites (ChatMessageView.test.tsx) — the console has no testing-library.
//
// The two guarantees the review rounds turned on, one `describe` each:
//   • the live preview renders from the streamed BUFFER while the tool's `input` is still "" — the
//     real-server shape (only the `code` arg streams; the full args arrive at model end). Keying the
//     preview on `JSON.parse(call.input)` left the operator on a spinner until the write finished;
//   • the WorkBlock forwards `renderFinal`, so on `done` the preview hands over to the artifact's own
//     inline frame instead of staying up as a dead preview beside a second, separate frame.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ChatPart, ToolCall } from "../lib/types";
import type { ToolArgsBuffer } from "./toolArgsBuffer";
import { WorkBlock } from "./WorkBlock";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(props: {
  parts: ChatPart[];
  toolCalls?: ToolCall[];
  toolArgs?: Record<string, ToolArgsBuffer>;
  streaming: boolean;
  renderFinal?: (height: number) => ReactNode;
}) {
  act(() => root.render(h(QueryClientProvider, { client: qc }, h(WorkBlock, props))));
}

function byTestId(id: string): HTMLElement | null {
  return container.querySelector(`[data-testid="${id}"]`);
}

// A reasoning + tool turn (so WorkBlock is the surface the console folds a streaming artifact into),
// streaming a single `show_artifact` whose declared `code` is arriving live.
const PARTS: ChatPart[] = [
  { kind: "reasoning", text: "Writing the page…" },
  { kind: "tools", ids: ["art-i"] },
];
// The real server leaves the call's `input` EMPTY all through the stream — only `code` streams.
const CALLS: ToolCall[] = [{ id: "art-i", name: "show_artifact", input: "", status: "running" }];
// The first chunk closes a <style>, so the preview gate is open and the markup reads as html.
const GATE_OPEN = '<style>#live{color:#09f}</style><div id="live">live preview';

describe("WorkBlock — streamed inline-artifact preview (r3, r1)", () => {
  it("renders the live preview from the buffer while the tool input is still empty", () => {
    render({
      parts: PARTS,
      toolCalls: CALLS,
      toolArgs: { "art-i": { arg: "code", text: GATE_OPEN, done: false } },
      streaming: true,
    });
    // The spotlight hosts the sandboxed preview + its frame — keyed on the buffer, NOT on `input`
    // (which is ""). The pre-fix code parsed `input` and showed nothing until the write was done.
    expect(byTestId("streaming-preview")).not.toBeNull();
    expect(byTestId("streaming-preview-frame")).not.toBeNull();
  });

  it("shows no preview for a streaming tool that is not a show_artifact", () => {
    render({
      parts: PARTS,
      toolCalls: [{ id: "art-i", name: "read_file", input: "", status: "running" }],
      toolArgs: { "art-i": { arg: "code", text: GATE_OPEN, done: false } },
      streaming: true,
    });
    expect(byTestId("streaming-preview")).toBeNull();
  });
});

describe("WorkBlock — handover forwards renderFinal (r2)", () => {
  it("swaps the preview for the final frame on done, at the last measured height", () => {
    const renderFinal = (height: number) =>
      h("div", { "data-testid": "final-frame", "data-height": String(height) }, "FINAL");
    // Done + a renderFinal on offer → the preview is gone, the final frame is in its place. (Absent
    // the forwarding, StreamingPreview never receives renderFinal and the preview just stays up.)
    render({
      parts: PARTS,
      toolCalls: [{ id: "art-i", name: "show_artifact", input: "", status: "done" }],
      toolArgs: { "art-i": { arg: "code", text: GATE_OPEN, done: true } },
      streaming: true,
      renderFinal,
    });
    expect(byTestId("streaming-preview")).toBeNull();
    const final = byTestId("final-frame");
    expect(final).not.toBeNull();
    // No frame ever reported a height, so the handover uses the preview's fixed starting height.
    expect(final!.getAttribute("data-height")).toBe("240");
  });
});
