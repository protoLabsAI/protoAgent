// The streamed inline-artifact preview (ADR 0118 D3 / S8c) must render on BOTH turn shapes (#4121):
// a FOLDED turn (reasoning + a tool call) hosts it in the WorkBlock spotlight, as before; an
// UNFOLDED turn (a `show_artifact` call with NO reasoning part — what Claude on the OAuth lane
// emits, since it sends no thinking) has no WorkBlock, so foldPlan keeps its tool card inline — and
// the preview, which only the WorkBlock used to mount, would never appear. ChatMessageView now
// drives the spotlit call through the SAME inlineArtifactPreview / StreamingPreview path on the
// unfolded path too, and hands it over to the real inline frame once the artifact-ref lands.
// createRoot/act, like ChatMessageView.test.tsx.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { registerChatComponent } from "../ext/componentRegistry";
import type { ChatMessage, ChatPart, ToolCall } from "../lib/types";
import { ChatMessageView } from "./ChatMessageView";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Same hermetic artifact-ref stub as ChatMessageView.test.tsx — echoes props so a test can count
// handover renders and read the height the S8c swap injects, without the Artifact panel.
registerChatComponent("artifact-ref", ({ props }) =>
  h("div", {
    "data-testid": "artifact-ref-stub",
    "data-artifact-id": String(props.artifact_id ?? ""),
    "data-inline": String(props.inline === true),
    "data-height": String(props.height ?? ""),
  }),
);

let container: HTMLElement;
let root: Root;
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function render(message: ChatMessage) {
  act(() => root.render(h(QueryClientProvider, { client: qc }, h(ChatMessageView, { message }))));
}

const reasoning: ChatPart = { kind: "reasoning", text: "Writing the page…" };
const tools: ChatPart = { kind: "tools", ids: ["art-i"] };
const ref: ChatPart = {
  kind: "component",
  spec: {
    component: "artifact-ref",
    props: { artifact_id: "art-inline", version: 1, versions_total: 1, title: "Streamed page", kind: "html", inline: true, height: 160 },
  },
};
const answer: ChatPart = { kind: "text", text: "Here's the streamed page." };
// A streaming `show_artifact` whose declared `code` is arriving as a tool-args buffer: a closed
// `<style>` opens the preview gate, and `html` markup is previewable (StreamingPreview).
const WRITING = '<style>#live{color:#09f}</style><div id="live">live';
const DONE_HTML = '<style>#live{color:#09f}</style><div id="live">live preview</div>';
const show = (status: ToolCall["status"]): ToolCall[] => [{ id: "art-i", name: "show_artifact", input: "", status }];

function artMsg(parts: ChatPart[], status: ChatMessage["status"], toolCalls: ToolCall[], toolArgs?: ChatMessage["toolArgs"]): ChatMessage {
  return { id: "a1", role: "assistant", content: "", status, parts, toolCalls, toolArgs };
}

const preview = () => container.querySelector('[data-testid="streaming-preview"]');
const refStubs = () => [...container.querySelectorAll('[data-testid="artifact-ref-stub"]')];

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

describe("ChatMessageView — streamed preview, folded path (reasoning present)", () => {
  it("mounts the live preview inside the WorkBlock spotlight", () => {
    render(artMsg([reasoning, tools], "streaming", show("running"), { "art-i": { arg: "code", text: WRITING, done: false } }));
    // The reason+tool turn folds, so the preview lives in the WorkBlock.
    const work = container.querySelector(".work");
    expect(work).not.toBeNull();
    expect(work!.querySelector('[data-testid="streaming-preview"]')).not.toBeNull();
  });
});

describe("ChatMessageView — streamed preview, unfolded path (no reasoning, #4121)", () => {
  it("mounts the live preview with NO WorkBlock when the turn has a tool call but no reasoning", () => {
    render(artMsg([tools], "streaming", show("running"), { "art-i": { arg: "code", text: WRITING, done: false } }));
    // The tool-only turn does NOT fold — there is no WorkBlock …
    expect(container.querySelector(".work")).toBeNull();
    // … yet the live preview still renders (the bug: it never mounted here), and the real inline
    // frame has not taken over yet.
    expect(preview()).not.toBeNull();
    expect(preview()!.closest(".work")).toBeNull();
    expect(refStubs()).toHaveLength(0);
  });

  it("hands the preview over to the real inline frame once the artifact-ref lands", () => {
    render(artMsg([tools, ref], "streaming", show("done"), { "art-i": { arg: "code", text: DONE_HTML, done: true } }));
    // The handover replaced the preview with the real frame — still no WorkBlock …
    expect(container.querySelector(".work")).toBeNull();
    expect(preview()).toBeNull();
    // … and the ref renders exactly once (pulled out of the answer so it doesn't stack below the
    // spotlight), seeded with the preview's starting height since jsdom measured none (no jump).
    const stubs = refStubs();
    expect(stubs).toHaveLength(1);
    expect(stubs[0].getAttribute("data-inline")).toBe("true");
    expect(stubs[0].getAttribute("data-height")).toBe("240");
  });

  it("once settled, the ref renders once in the answer, with no spotlight and no preview", () => {
    // toolArgs are dropped when the turn settles (ADR 0118 D3), and streaming is false.
    render(artMsg([tools, ref, answer], "done", show("done")));
    expect(container.querySelector(".work-spotlight")).toBeNull();
    expect(preview()).toBeNull();
    const stubs = refStubs();
    expect(stubs).toHaveLength(1);
    // The answer owns the ref now, at its own height hint — the spotlight no longer seeds it.
    expect(stubs[0].getAttribute("data-height")).toBe("160");
  });
});
