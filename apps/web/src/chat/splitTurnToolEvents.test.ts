// #4300 — a tool row kept spinning after its result came back when it shared a step with
// background `delegate_to` calls. The step's three parallel starts land on the live bubble;
// the first delegation's room bubble SPLITS the turn (freezing those cards in a `splitOf`
// half); the sibling's end then arrives within milliseconds — after the split.
import { describe, expect, it } from "vitest";

import type { ChatMessage, ToolEvent } from "../lib/types";
import { insertRoomBubble } from "./roomBubble";
import { applyToolEvent, applyToolEventToTurn, settleSplitHalves, settleStreamEnd } from "./turnReducers";

const LIVE = "assistant-live";

function start(id: string, name: string, input = "{}"): ToolEvent {
  return { id, name, phase: "start", input };
}
function end(id: string, name: string, output: string): ToolEvent {
  return { id, name, phase: "end", output };
}

/** The step: atlas_show + two background delegate_to, all starting, then the first
 *  delegation's ask bubble splitting the turn, then every end landing. */
function runStep(apply: (messages: ChatMessage[], evt: ToolEvent) => ChatMessage[]): ChatMessage[] {
  let messages: ChatMessage[] = [
    { id: "u1", role: "user", content: "show the fleet", status: "done" },
    { id: LIVE, role: "assistant", content: "", parts: [], status: "streaming" },
  ];
  messages = apply(messages, start("call-atlas", "atlas_show", '{"view":"fleet_delegation"}'));
  messages = apply(messages, start("call-d1", "delegate_to", '{"to":"vera","background":true}'));
  messages = apply(messages, start("call-d2", "delegate_to", '{"to":"frank","background":true}'));
  // delegate_to(background=True) fires its room ask mid-step → the live bubble splits.
  messages = insertRoomBubble(
    messages,
    LIVE,
    { id: "ask-1", role: "assistant", content: "Vera, take this", status: "done", addressedTo: "vera" },
    "frozen-1",
  );
  messages = apply(messages, end("call-d1", "delegate_to", "started in background"));
  messages = apply(messages, end("call-d2", "delegate_to", "started in background"));
  messages = apply(messages, end("call-atlas", "atlas_show", "Showing view fleet_delegation … step 2 of 14"));
  return messages;
}

function card(messages: ChatMessage[], id: string) {
  return messages.flatMap((m) => (m.toolCalls ?? []).map((c) => ({ bubble: m.id, call: c }))).filter((x) => x.call.id === id);
}

describe("parallel tool calls across a delegation split (#4300)", () => {
  it("live-bubble-only application leaves the sibling spinning (the bug)", () => {
    const messages = runStep((ms, evt) => ms.map((m) => (m.id === LIVE ? applyToolEvent(m, evt) : m)));
    const atlas = card(messages, "call-atlas");
    // The frozen card never hears its end, and a duplicate ✓ appears in the continuation.
    expect(atlas.find((x) => x.bubble === "frozen-1")?.call.status).toBe("running");
    expect(atlas).toHaveLength(2);
  });

  it("each end settles the card where it lives — no spinner, no duplicate row", () => {
    const messages = runStep((ms, evt) => applyToolEventToTurn(ms, LIVE, evt));
    for (const id of ["call-atlas", "call-d1", "call-d2"]) {
      const found = card(messages, id);
      expect(found).toHaveLength(1);
      expect(found[0].bubble).toBe("frozen-1");
      expect(found[0].call.status).toBe("done");
    }
    expect(card(messages, "call-atlas")[0].call.output).toContain("step 2 of 14");
    // The continuation stays empty: nothing was drawn there twice.
    expect(messages.find((m) => m.id === LIVE)?.toolCalls ?? []).toHaveLength(0);
  });

  it("a later call after the split still lands on the live bubble", () => {
    let messages = runStep((ms, evt) => applyToolEventToTurn(ms, LIVE, evt));
    messages = applyToolEventToTurn(messages, LIVE, start("call-next", "read_file"));
    messages = applyToolEventToTurn(messages, LIVE, end("call-next", "read_file", "ok"));
    expect(card(messages, "call-next")).toEqual([{ bubble: LIVE, call: expect.objectContaining({ status: "done" }) }]);
  });

  it("a subagent's child start nests in the half holding its parent task card", () => {
    let messages: ChatMessage[] = [{ id: LIVE, role: "assistant", content: "", parts: [], status: "streaming" }];
    messages = applyToolEventToTurn(messages, LIVE, start("task-1", "task"));
    messages = insertRoomBubble(messages, LIVE, { id: "ask", role: "assistant", content: "hi", status: "done" }, "frozen-1");
    messages = applyToolEventToTurn(messages, LIVE, { ...start("child-1", "web_search"), parentId: "task-1" });
    expect(card(messages, "child-1")).toEqual([{ bubble: "frozen-1", call: expect.objectContaining({ parentId: "task-1" }) }]);
  });
});

describe("settleSplitHalves — the defensive turn-end settle (#4300)", () => {
  function splitWithRunning(): ChatMessage[] {
    let messages: ChatMessage[] = [{ id: LIVE, role: "assistant", content: "", parts: [], status: "streaming" }];
    messages = messages.map((m) => applyToolEvent(m, start("call-atlas", "atlas_show")));
    return insertRoomBubble(messages, LIVE, { id: "ask", role: "assistant", content: "hi", status: "done" }, "frozen-1");
  }

  it("a card still running in a frozen half settles done when the turn ends", () => {
    const settled = settleSplitHalves(
      splitWithRunning().map((m) => (m.id === LIVE ? settleStreamEnd(m, { parked: false }) : m)),
      LIVE,
    );
    const atlas = card(settled, "call-atlas")[0];
    expect(atlas.call.status).toBe("done");
    expect(atlas.call.stopped).toBeUndefined();
  });

  it("a stopped turn settles them STOPPED, and leaves other turns alone", () => {
    const other: ChatMessage = {
      id: "old",
      role: "assistant",
      content: "",
      status: "done",
      splitOf: "another-turn",
      toolCalls: [{ id: "x", name: "t", status: "running" }],
    };
    const settled = settleSplitHalves([other, ...splitWithRunning()], LIVE, { stopped: true });
    expect(card(settled, "call-atlas")[0].call).toMatchObject({ status: "done", stopped: true });
    expect(card(settled, "x")[0].call.status).toBe("running");
  });

  it("returns the same array when nothing needs settling", () => {
    const messages: ChatMessage[] = [{ id: LIVE, role: "assistant", content: "", status: "done" }];
    expect(settleSplitHalves(messages, LIVE)).toBe(messages);
  });
});
