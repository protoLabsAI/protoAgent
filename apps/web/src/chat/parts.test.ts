import { describe, expect, it } from "vitest";

import type { ChatPart, ToolCall } from "../lib/types";
import { addComponent, addToolRef, appendReasoning, appendText, foldPlan, toolsForGroup } from "./parts";

describe("addComponent", () => {
  it("appends a component part at its emission point (before the answer text streams in)", () => {
    let p: ChatPart[] | undefined;
    p = appendReasoning(p, "let me build a table");
    p = addComponent(p, { component: "table", props: { rows: [] } });
    p = appendText(p, "here it is", false); // answer streams AFTER the component
    expect(p).toEqual([
      { kind: "reasoning", text: "let me build a table" },
      { kind: "component", spec: { component: "table", props: { rows: [] } } },
      { kind: "text", text: "here it is" },
    ]);
  });
});

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

describe("foldPlan", () => {
  const reasoning = (text: string): ChatPart => ({ kind: "reasoning", text });
  const text = (t: string): ChatPart => ({ kind: "text", text: t });
  const tools = (...ids: string[]): ChatPart => ({ kind: "tools", ids });
  const component = (): ChatPart => ({ kind: "component", spec: { component: "x", props: {} } });

  it("folds a reason+tool turn and splits the trailing answer once settled", () => {
    const parts = [reasoning("think"), tools("a"), text("the answer")];
    expect(foldPlan(parts, false)).toEqual({
      fold: true,
      workParts: [reasoning("think"), tools("a")],
      answerParts: [text("the answer")],
    });
  });

  it("keeps an ambiguous trailing text run as WORK while streaming a folded turn (the flash guard)", () => {
    // Interstitial narration after a tool, mid-turn — must NOT become the answer yet, or it
    // flashes into the main chat then jumps back into the WorkBlock when the next tool arrives.
    const parts = [reasoning("think"), tools("a"), text("let me try another tool")];
    expect(foldPlan(parts, true)).toEqual({ fold: true, workParts: parts, answerParts: [] });
  });

  it("keeps a trailing component as work while streaming a folded turn", () => {
    const parts = [reasoning("think"), tools("a"), component()];
    expect(foldPlan(parts, true)).toEqual({ fold: true, workParts: parts, answerParts: [] });
  });

  it("does NOT fold a tool-only turn — a simple tool result keeps its card inline (no reasoning to batch)", () => {
    const parts = [tools("a"), text("answer")];
    // No reasoning → not folded; the normal split applies, streaming or settled. The web_search
    // card renders directly rather than collapsing behind a "Worked" summary.
    expect(foldPlan(parts, true)).toEqual({ fold: false, workParts: [tools("a")], answerParts: [text("answer")] });
    expect(foldPlan(parts, false)).toEqual({ fold: false, workParts: [tools("a")], answerParts: [text("answer")] });
  });

  it("does NOT fold a tool+narration turn without reasoning — reverts to the inline render", () => {
    // tools + interim narration, no reasoning part → not folded; renders inline (the pre-#1417
    // behaviour). Trailing part is a tool, so everything is work and nothing is deferred.
    const parts = [tools("a"), text("running the next one"), tools("b")];
    expect(foldPlan(parts, true)).toEqual({ fold: false, workParts: parts, answerParts: [] });
  });

  it("does NOT fold a reasoning-only turn (no tools)", () => {
    const parts = [reasoning("think"), text("answer")];
    expect(foldPlan(parts, true)).toEqual({ fold: false, workParts: [reasoning("think")], answerParts: [text("answer")] });
  });

  it("a plain text turn is all answer, never folded", () => {
    expect(foldPlan([text("hi")], true)).toEqual({ fold: false, workParts: [], answerParts: [text("hi")] });
  });

  it("a folded turn with no answer yet keeps everything as work, settled or streaming", () => {
    const parts = [reasoning("think"), tools("a")];
    expect(foldPlan(parts, false)).toEqual({ fold: true, workParts: parts, answerParts: [] });
    expect(foldPlan(parts, true)).toEqual({ fold: true, workParts: parts, answerParts: [] });
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
