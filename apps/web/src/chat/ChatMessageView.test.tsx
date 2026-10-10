// Visible text is never yanked. A reasoning model's turn streams `reasoning → "protoAgent is…"`
// into the bubble unfolded, then calls a tool — which completes the reason+tool pair and folds the
// turn into the WorkBlock. The sentence used to vanish into the collapsed "Working…" block at that
// instant (the launch-demo glitch). These render the real ChatMessageView through that sequence.
// createRoot/act, like the other console UI suites.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { registerChatComponent } from "../ext/componentRegistry";
import type { ChatMessage, ChatPart, ToolCall } from "../lib/types";
import { ChatMessageView } from "./ChatMessageView";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// A hermetic stand-in for the artifact-ref renderer (the real one is registered by an app bootstrap
// the unit env doesn't load). Echoes the props so a test can count the renders AND read the height
// the S8c handover injects — without depending on the Artifact panel being available in the store.
registerChatComponent("artifact-ref", ({ props }) =>
  h("div", {
    "data-testid": "artifact-ref-stub",
    "data-inline": String(props.inline === true),
    "data-height": String(props.height ?? ""),
  }),
);

const SENTENCE = "protoAgent is a private, plugin-extensible desktop agent.";
const ANSWER = "Done — the three bullets are in your notes.";

let container: HTMLElement;
let root: Root;
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function render(message: ChatMessage) {
  act(() => root.render(h(QueryClientProvider, { client: qc }, h(ChatMessageView, { message }))));
}

function msg(parts: ChatPart[], status: ChatMessage["status"], toolCalls?: ToolCall[]): ChatMessage {
  return { id: "a1", role: "assistant", content: "", status, parts, toolCalls };
}

/** Elements whose own text is exactly `text`, rendered OUTSIDE the folded WorkBlock — i.e. on
 *  screen in the bubble rather than behind the collapsed "Working…/Worked" disclosure. */
function visibleOutsideWork(text: string): Element[] {
  return [...container.querySelectorAll("p, span, div")].filter(
    (el) => el.textContent?.trim() === text && el.children.length === 0 && !el.closest(".work"),
  );
}

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

describe("ChatMessageView — pre-tool text stays put when the turn folds", () => {
  const reasoning: ChatPart = { kind: "reasoning", text: "The operator wants a summary and a note." };
  const sentence: ChatPart = { kind: "text", text: SENTENCE };
  const tools: ChatPart = { kind: "tools", ids: ["note-1"] };
  const running: ToolCall[] = [{ id: "note-1", name: "append_note", status: "running" }];
  const done: ToolCall[] = [{ id: "note-1", name: "append_note", status: "done" }];

  it("keeps the streamed sentence rendered — the same DOM node — after tool_start folds the turn", () => {
    render(msg([reasoning, sentence], "streaming"));
    const [before] = visibleOutsideWork(SENTENCE);
    expect(before).toBeDefined();

    // tool_start: the reason+tool turn folds behind the WorkBlock.
    render(msg([reasoning, sentence, tools], "streaming", running));
    expect(container.querySelector(".work")).not.toBeNull();
    const after = visibleOutsideWork(SENTENCE);
    expect(after).toHaveLength(1);
    // Not remounted: the node the viewer was reading is the node still on screen.
    expect(after[0]).toBe(before);
    // …and it sits ABOVE the WorkBlock (emission order: preamble, then the tool work).
    const work = container.querySelector(".work")!;
    expect(after[0].compareDocumentPosition(work) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("the final answer lands below without losing or duplicating the sentence", () => {
    render(msg([reasoning, sentence], "streaming"));
    render(msg([reasoning, sentence, tools], "streaming", running));
    render(msg([reasoning, sentence, tools, { kind: "text", text: ANSWER }], "done", done));

    expect(visibleOutsideWork(SENTENCE)).toHaveLength(1);
    expect(visibleOutsideWork(ANSWER)).toHaveLength(1);
    // The sentence is not ALSO folded into the work timeline (no duplicate behind the disclosure).
    expect(container.querySelector(".work")!.textContent).not.toContain(SENTENCE);
    const [s] = visibleOutsideWork(SENTENCE);
    const [a] = visibleOutsideWork(ANSWER);
    expect(s.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe("ChatMessageView — streaming inline artifact hands over without doubling (S8c)", () => {
  // The inline artifact-ref (S7b) the streaming show_artifact hands its preview over to.
  const ref: ChatPart = {
    kind: "component",
    spec: {
      component: "artifact-ref",
      props: { artifact_id: "art-inline", version: 1, versions_total: 1, title: "Streamed page", kind: "html", inline: true, height: 160 },
    },
  };
  const reasoning: ChatPart = { kind: "reasoning", text: "Writing the page…" };
  const tools: ChatPart = { kind: "tools", ids: ["art-i"] };
  const refStubs = () => [...container.querySelectorAll('[data-testid="artifact-ref-stub"]')];

  it("while streaming, the landed ref renders exactly once — in the spotlight handover, not the answer", () => {
    const streamingDone: ChatMessage = {
      id: "a1",
      role: "assistant",
      content: "",
      status: "streaming",
      parts: [reasoning, tools, ref],
      toolCalls: [{ id: "art-i", name: "show_artifact", input: "", status: "done" }],
      toolArgs: { "art-i": { arg: "code", text: '<style>#x{color:red}</style><div id="x">live', done: true } },
    };
    render(streamingDone);
    // The handover replaced the preview with the real frame — no preview card is left up …
    expect(container.querySelector('[data-testid="streaming-preview"]')).toBeNull();
    // … and the ref renders exactly ONCE (pulled out of the answer so it doesn't stack below the
    // spotlight), seeded with the preview's starting height since no frame measured one (no jump).
    const stubs = refStubs();
    expect(stubs).toHaveLength(1);
    expect(stubs[0].getAttribute("data-inline")).toBe("true");
    expect(stubs[0].getAttribute("data-height")).toBe("240");
  });

  it("once the turn settles, the ref renders once in the answer and the spotlight is gone", () => {
    const settled: ChatMessage = {
      id: "a1",
      role: "assistant",
      content: "",
      status: "done",
      parts: [reasoning, tools, ref, { kind: "text", text: "Here's the streamed page." }],
      toolCalls: [{ id: "art-i", name: "show_artifact", input: "", status: "done" }],
      // toolArgs are dropped when the turn settles (ADR 0118 D3).
    };
    render(settled);
    expect(container.querySelector(".work-spotlight")).toBeNull();
    const stubs = refStubs();
    expect(stubs).toHaveLength(1);
    // Now the answer owns it, at its own height hint — the spotlight no longer seeds the height.
    expect(stubs[0].getAttribute("data-height")).toBe("160");
  });
});

describe("ChatMessageView — a dangling markdown marker never paints while streaming", () => {
  const md = () => [...container.querySelectorAll(".markdown")].map((el) => el.textContent ?? "").join("|");

  it("holds back a bare `**` first delta, then renders the bold once it has content", () => {
    render(msg([{ kind: "text", text: "**" }], "streaming"));
    expect(md()).not.toContain("**");

    render(msg([{ kind: "text", text: "Done:\n\n- **" }], "streaming"));
    expect(md()).not.toContain("**");
    expect(md()).toContain("Done:");

    render(msg([{ kind: "text", text: "Done:\n\n- **protoAgent" }], "streaming"));
    expect(container.querySelector('.markdown [data-streamdown="strong"]')?.textContent).toBe("protoAgent");
  });

  it("only the still-streaming LAST part is trimmed; settled text renders verbatim", () => {
    const tools: ChatPart = { kind: "tools", ids: ["t1"] };
    const calls: ToolCall[] = [{ id: "t1", name: "append_note", status: "done" }];
    render(msg([{ kind: "text", text: "Rate: 5 **" }, tools, { kind: "text", text: "`" }], "streaming", calls));
    expect(md()).toContain("Rate: 5 **");
    expect(md()).not.toContain("`");

    render(msg([{ kind: "text", text: "Rate: 5 **" }, tools, { kind: "text", text: "`" }], "done", calls));
    expect(md()).toContain("`");
  });
});
