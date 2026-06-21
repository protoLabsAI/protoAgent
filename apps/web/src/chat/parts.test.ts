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

  it("skips a whitespace-only delta between tools, keeping the group open", () => {
    // The "left a space" bug: a stray "\n" between two tool calls became an empty
    // text part (a rendered gap) AND split the tool group.
    let p: ChatPart[] | undefined = [{ kind: "tools", ids: ["a"] }];
    p = appendText(p, "\n", true);
    p = addToolRef(p, "b"); // extends the SAME group — no empty text part in between
    expect(p).toEqual([{ kind: "tools", ids: ["a", "b"] }]);
  });

  it("trims leading whitespace when a text run starts after a tool group", () => {
    let p: ChatPart[] | undefined = [{ kind: "tools", ids: ["a"] }];
    p = appendText(p, "\n\nHere's the answer", true);
    expect(p).toEqual([
      { kind: "tools", ids: ["a"] },
      { kind: "text", text: "Here's the answer" },
    ]);
  });
});

describe("appendReasoning", () => {
  it("extends the open reasoning run on consecutive deltas", () => {
    let p: ChatPart[] | undefined;
    p = appendReasoning(p, "Let me ");
    p = appendReasoning(p, "think");
    expect(p).toEqual([{ kind: "reasoning", text: "Let me think" }]);
  });

  it("interleaves reasoning between tools (reason · tool · reason · answer)", () => {
    let p: ChatPart[] | undefined;
    p = appendReasoning(p, "First I'll search");
    p = addToolRef(p, "web_search");
    p = appendReasoning(p, "Now I'll read it"); // resumes thinking after the tool — inline
    p = addToolRef(p, "fetch_url");
    p = appendText(p, "Here's the answer", true);
    expect(p).toEqual([
      { kind: "reasoning", text: "First I'll search" },
      { kind: "tools", ids: ["web_search"] },
      { kind: "reasoning", text: "Now I'll read it" },
      { kind: "tools", ids: ["fetch_url"] },
      { kind: "text", text: "Here's the answer" },
    ]);
  });

  it("starts a fresh reasoning run after text/tools, trimming leading whitespace", () => {
    let p: ChatPart[] | undefined = [{ kind: "tools", ids: ["a"] }];
    p = appendReasoning(p, "\n\nreconsidering");
    expect(p).toEqual([
      { kind: "tools", ids: ["a"] },
      { kind: "reasoning", text: "reconsidering" },
    ]);
  });

  it("skips a pure-whitespace reasoning delta after a tool group", () => {
    let p: ChatPart[] | undefined = [{ kind: "tools", ids: ["a"] }];
    p = appendReasoning(p, "\n");
    expect(p).toEqual([{ kind: "tools", ids: ["a"] }]);
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
