// A coding delegate's reply rendered as it happened. The reply text alone is the coder's
// narration with every action cut out ("Now executing the edits:" … "The tests pass."), so
// the server sends the run in order on the reply frame (graph/delegate_progress.py) and the
// bubble renders text ↔ tool cards like a native turn. Pinned here: the wire decoder
// (defensive), the bubble builder, the room-frame decode, and the real ChatMessageView
// placing each tool card between the sentences around it.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { roomReplyFromParts } from "../lib/api/a2aStream";
import { delegateTimelineFromWire, delegateTimelineMessage } from "../lib/delegateProgress";
import type { ChatMessage } from "../lib/types";
import { ChatMessageView } from "./ChatMessageView";
import { isDirectAddressCard } from "./ToolCalls";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ROOM_MIME = "application/vnd.protolabs.room-v1+json";

const WIRE = [
  { type: "text", text: "Now executing the edits:" },
  { type: "tool", id: "t1", name: "Edit calc.py", kind: "edit", status: "completed", input: '{"file_path":"calc.py"}' },
  { type: "tool", id: "t2", name: "Edit test_calc.py", kind: "edit", status: "completed" },
  { type: "text", text: "Let me run the test suite to confirm:" },
  { type: "tool", id: "t3", name: "python -m pytest -q", kind: "execute", status: "failed", output: "1 failed" },
  { type: "text", text: "All 3 tests pass." },
];

describe("delegateTimelineFromWire", () => {
  it("decodes the run in order", () => {
    const tl = delegateTimelineFromWire(WIRE)!;
    expect(tl.map((e) => (e.type === "text" ? e.text : e.name))).toEqual([
      "Now executing the edits:",
      "Edit calc.py",
      "Edit test_calc.py",
      "Let me run the test suite to confirm:",
      "python -m pytest -q",
      "All 3 tests pass.",
    ]);
  });

  it("refuses anything it cannot render faithfully", () => {
    expect(delegateTimelineFromWire(undefined)).toBeUndefined();
    expect(delegateTimelineFromWire("x")).toBeUndefined();
    // No tool call: the reply text already is the whole story.
    expect(delegateTimelineFromWire([{ type: "text", text: "hi" }])).toBeUndefined();
    // An unknown entry would drop part of the run — the plain text is safer.
    expect(delegateTimelineFromWire([...WIRE, { type: "mystery" }])).toBeUndefined();
    expect(delegateTimelineFromWire([{ type: "tool", id: "x" }])).toBeUndefined();
  });

  it("rides the room frame only on a reply", () => {
    const reply = roomReplyFromParts([{ metadata: { mimeType: ROOM_MIME }, data: { author: "claude-code", text: "x", timeline: WIRE } }]);
    expect(reply?.timeline).toHaveLength(6);
    const ask = roomReplyFromParts([{ metadata: { mimeType: ROOM_MIME }, data: { addressed_to: "claude-code", text: "x", timeline: WIRE } }]);
    expect(ask?.timeline).toBeUndefined();
  });
});

describe("delegateTimelineMessage", () => {
  it("groups adjacent calls and scopes their ids to the bubble", () => {
    const { parts, toolCalls } = delegateTimelineMessage(delegateTimelineFromWire(WIRE)!, "m1");
    expect(parts.map((p) => p.kind)).toEqual(["text", "tools", "text", "tools", "text"]);
    expect(parts[1]).toEqual({ kind: "tools", ids: ["m1:t1:0", "m1:t2:1"] });
    expect(toolCalls.map((c) => c.status)).toEqual(["done", "done", "error"]);
    expect(toolCalls[0].input).toBe('{"file_path":"calc.py"}');
  });
});

describe("a delegate reply bubble", () => {
  let container: HTMLElement;
  let root: Root;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as never;
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("puts each tool card between the sentences around it", () => {
    const message: ChatMessage = {
      id: "m1",
      role: "assistant",
      status: "done",
      author: { name: "claude-code" },
      content: "Now executing the edits:\n\nLet me run the test suite to confirm:\n\nAll 3 tests pass.",
      ...delegateTimelineMessage(delegateTimelineFromWire(WIRE)!, "m1"),
    };
    act(() => root.render(h(QueryClientProvider, { client: qc }, h(ChatMessageView, { message }))));
    // Adjacent calls fold into one group; open it so its cards' names are in the DOM.
    const group = container.querySelector<HTMLButtonElement>(".pl-toolcard-summary__head");
    act(() => group?.click());
    const text = container.textContent ?? "";
    const at = (s: string) => {
      const i = text.indexOf(s);
      expect(i, s).toBeGreaterThanOrEqual(0);
      return i;
    };
    expect(at("Now executing the edits:")).toBeLessThan(at("Edit calc.py"));
    expect(at("Edit test_calc.py")).toBeLessThan(at("Let me run the test suite"));
    expect(at("Let me run the test suite")).toBeLessThan(at("python -m pytest -q"));
    expect(at("python -m pytest -q")).toBeLessThan(at("All 3 tests pass."));
    // The narration renders once — never the parts AND the flat content.
    expect(text.split("All 3 tests pass.").length - 1).toBe(1);
    // Claude Code's tools are not ours: no Tools-settings link on them.
    expect(container.querySelector(".tool-manage-btn")).toBeNull();
  });
});

describe("isDirectAddressCard", () => {
  it("knows the cards a direct address opens are not tools", () => {
    for (const id of ["mention:claude-code", "workflow:triage", "subagent:researcher"])
      expect(isDirectAddressCard({ id, name: id, status: "done" })).toBe(true);
    expect(isDirectAddressCard({ id: "call_1", name: "web_search", status: "done" })).toBe(false);
  });
});
