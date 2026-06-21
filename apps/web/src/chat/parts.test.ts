import { describe, expect, it } from "vitest";

import type { ChatPart, ToolCall } from "../lib/types";
import { addToolRef, appendReasoning, appendText, toolsForGroup } from "./parts";

describe("appendText", () => {
  it("starts a run, then appends deltas to it", () => {
    let p: ChatPart[] | undefined;
    p = appendText(p, "Let me ", false); // first frame (server sends append=false)
    p = appendText(p, "search", true);
    p = appendText(p, ".", true);
    expect(p).toEqual([{ kind: "text", text: "Let me search." }]);
  });

  it("replaces the open run when append=false (terminal non-streamed answer)", () => {
    let p: ChatPart[] | undefined = [{ kind: "text", text: "partial" }];
    p = appendText(p, "the whole answer", false);
    expect(p).toEqual([{ kind: "text", text: "the whole answer" }]);
  });

  it("starts a NEW text run after a tool group, even on append=true", () => {
    let p: ChatPart[] | undefined;
    p = appendText(p, "preamble", false);
    p = addToolRef(p, "t1");
    p = appendText(p, "answer", true); // post-tool delta — must not extend the preamble
    expect(p).toEqual([
      { kind: "text", text: "preamble" },
      { kind: "tools", ids: ["t1"] },
      { kind: "text", text: "answer" },
    ]);
  });
});

describe("addToolRef", () => {
  it("groups consecutive tool calls into one block", () => {
    let p: ChatPart[] | undefined;
    p = appendText(p, "searching", false);
    p = addToolRef(p, "web_search");
    p = addToolRef(p, "fetch_url");
    expect(p).toEqual([
      { kind: "text", text: "searching" },
      { kind: "tools", ids: ["web_search", "fetch_url"] },
    ]);
  });

  it("is idempotent on a repeated id", () => {
    let p = addToolRef(undefined, "t1");
    p = addToolRef(p, "t1");
    expect(p).toEqual([{ kind: "tools", ids: ["t1"] }]);
  });

  it("opens a fresh group when text intervenes between tools", () => {
    let p: ChatPart[] | undefined = [{ kind: "tools", ids: ["a"] }];
    p = appendText(p, "mid", false);
    p = addToolRef(p, "b");
    expect(p).toEqual([
      { kind: "tools", ids: ["a"] },
      { kind: "text", text: "mid" },
      { kind: "tools", ids: ["b"] },
    ]);
  });
});

describe("appendReasoning", () => {
  it("extends the open reasoning run on each delta", () => {
    let p: ChatPart[] | undefined;
    p = appendReasoning(p, "Thinking");
    p = appendReasoning(p, " about it");
    expect(p).toEqual([{ kind: "reasoning", text: "Thinking about it" }]);
  });

  it("starts a NEW reasoning block after a tool (one per step), trimming the join separator", () => {
    let p: ChatPart[] | undefined;
    p = appendReasoning(p, "step 1 thinking");
    p = addToolRef(p, "t1");
    p = appendReasoning(p, "\n\nstep 2 thinking"); // server joins steps with a blank line
    expect(p).toEqual([
      { kind: "reasoning", text: "step 1 thinking" },
      { kind: "tools", ids: ["t1"] },
      { kind: "reasoning", text: "step 2 thinking" },
    ]);
  });

  it("interleaves reasoning · tools · reasoning · answer in emission order", () => {
    let p: ChatPart[] | undefined;
    p = appendReasoning(p, "let me search");
    p = addToolRef(p, "web");
    p = appendReasoning(p, "\n\nnow I know");
    p = appendText(p, "Here's the answer.", true);
    expect(p.map((x) => x.kind)).toEqual(["reasoning", "tools", "reasoning", "text"]);
  });
});

describe("toolsForGroup", () => {
  const calls: ToolCall[] = [
    { id: "task1", name: "task", status: "running" },
    { id: "child1", name: "web_search", status: "done", parentId: "task1" },
    { id: "other", name: "fetch_url", status: "done" },
  ];

  it("returns a group's top-level calls plus their nested children", () => {
    expect(toolsForGroup(["task1"], calls).map((c) => c.id)).toEqual(["task1", "child1"]);
  });

  it("excludes tools from other groups", () => {
    expect(toolsForGroup(["other"], calls).map((c) => c.id)).toEqual(["other"]);
  });

  it("is empty-safe", () => {
    expect(toolsForGroup(["x"], undefined)).toEqual([]);
  });
});
