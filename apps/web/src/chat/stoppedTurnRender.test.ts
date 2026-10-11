// #4076: the operator pressed Stop while `show_artifact` streamed. After a reload the cancelled
// card showed a green ✓ (with empty args) and nothing said the turn was stopped — the Stop
// settle, durable hydration and reattach all flipped an unfinished call to plain `done`. This
// pins the RENDER: an unfinished call in a stopped turn reads "stopped" (no ✓, no spinner), a
// call that did return keeps its ✓, and the bubble's footer carries a "Stopped" marker — both
// on the live Stop settle and on a cold rebuild of a `canceled` durable turn.
// (Same jsdom mount pattern as pausedTurnRender.test.ts.)
import { afterEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { DurableChatTurn } from "../lib/api";
import type { ChatMessage } from "../lib/types";
import { ChatMessageView } from "./ChatMessageView";
import { messagesFromDurableTurn } from "./sessionHydration";
import { finalizeStoppedMessages } from "./stopTurn";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TOOL = "https://proto-labs.ai/a2a/ext/tool-call-v1";

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(message: ChatMessage): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(ChatMessageView, { message }));
  });
  return host;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

/** The card whose header names `tool`. */
function card(el: HTMLElement, tool: string): Element {
  const found = [...el.querySelectorAll(".pl-toolcard")].find((c) =>
    c.querySelector(".pl-toolcard__name")?.textContent?.includes(tool),
  );
  if (!found) throw new Error(`no card for ${tool}`);
  return found;
}

async function assertStoppedRender(el: HTMLElement) {
  // The settled turn folds its cards into the summary chip — whose ✓ is hidden too, since the
  // block did not all succeed. Expand it to reach the cards.
  const summary = el.querySelector(".pl-toolcard-summary");
  expect(summary?.classList.contains("tool-stopped")).toBe(true);
  await act(async () => (summary!.querySelector(".pl-toolcard-summary__head") as HTMLElement).click());
  const stopped = card(el, "show_artifact");
  // Settled (no spinner), and the card says stopped — the DS ✓ is hidden by .tool-stopped.
  expect(stopped.querySelector(".pl-toolcard__status--running")).toBeNull();
  expect(stopped.classList.contains("tool-stopped")).toBe(true);
  expect(stopped.querySelector(".tool-stopped-label")?.textContent).toContain("stopped");
  // The call that DID return keeps its ✓ and no stopped chip.
  const finished = card(el, "load_skill");
  expect(finished.classList.contains("tool-stopped")).toBe(false);
  expect(finished.querySelector(".tool-stopped-label")).toBeNull();
  expect(finished.querySelector(".pl-toolcard__status--done")).not.toBeNull();
  // The turn says it was stopped.
  expect(el.querySelector('[data-testid="chat-stopped-mark"]')?.textContent).toContain("Stopped");
}

describe("a stopped turn renders stopped, not ✓ success (#4076)", () => {
  it("live Stop: the in-flight card settles stopped and the bubble is marked", async () => {
    const [settled] = finalizeStoppedMessages([
      {
        id: "a1",
        role: "assistant",
        content: "",
        status: "streaming",
        taskId: "t1",
        createdAt: Date.now(),
        toolCalls: [
          { id: "c1", name: "load_skill", status: "done", durationMs: 3 },
          { id: "c2", name: "show_artifact", status: "running", startedAt: Date.now() - 2_000 },
        ],
        parts: [{ kind: "tools", ids: ["c1", "c2"] }],
      },
    ]);
    await assertStoppedRender(await render(settled));
  });

  it("reload: a canceled durable turn rebuilds with the same stopped render", async () => {
    const turn: DurableChatTurn = {
      task_id: "t1",
      state: "TASK_STATE_CANCELED",
      last_updated: "2026-10-10T06:12:00Z",
      text: "",
      status: { state: "TASK_STATE_CANCELED" },
      artifacts: [],
      history: [
        { role: "ROLE_USER", parts: [{ text: "Make me an interactive mortgage table" }] },
        { role: "ROLE_AGENT", parts: [], metadata: { [TOOL]: { toolCallId: "c1", name: "load_skill", phase: "started", args: "{}" } } },
        { role: "ROLE_AGENT", parts: [], metadata: { [TOOL]: { toolCallId: "c1", name: "load_skill", phase: "completed", result: "ok" } } },
        { role: "ROLE_AGENT", parts: [], metadata: { [TOOL]: { toolCallId: "c2", name: "show_artifact", phase: "started", args: "" } } },
      ],
    };
    const assistant = messagesFromDurableTurn(turn).find((m) => m.role === "assistant")!;
    await assertStoppedRender(await render(assistant));
  });

  it("control: a completed turn shows no stopped marker", async () => {
    const el = await render({
      id: "a1",
      role: "assistant",
      content: "done",
      status: "done",
      createdAt: Date.now(),
      toolCalls: [{ id: "c1", name: "load_skill", status: "done", durationMs: 3 }],
      parts: [{ kind: "tools", ids: ["c1"] }, { kind: "text", text: "done" }],
    });
    expect(el.querySelector(".tool-stopped-label")).toBeNull();
    expect(el.querySelector('[data-testid="chat-stopped-mark"]')).toBeNull();
  });
});
