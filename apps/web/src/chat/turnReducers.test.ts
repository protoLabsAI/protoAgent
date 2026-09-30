// #3956: the LIVE stream's close on a turn parked on the operator. The SDK ends
// SendStreamingMessage at input-required, and the console used to settle that close like a
// finished turn: the in-flight `ask_human` card flipped to done ✓ while the form was up, and
// the persisted bubble read "done", so a reload in the same browser had nothing to reattach
// and the form never came back. `settleStreamEnd` is the one place that close is decided.

import { describe, expect, it } from "vitest";

import type { DurableChatTurn } from "../lib/api";
import type { ChatMessage } from "../lib/types";
import { reattachKeyForMessages, settleAnsweredPause, shouldReattach } from "./reattach";
import { messagesFromDurableTurn } from "./sessionHydration";
import { makeA2ADispatcher } from "../lib/api/a2aStream";
import { applyToolEvent, createParkTracker, isParkedState, settleStreamEnd, unpauseBubble } from "./turnReducers";

const TOOL = "https://proto-labs.ai/a2a/ext/tool-call-v1";

/** The live bubble at the moment the stream parks: `ask_human` started, never ended. */
function parkedLive(): ChatMessage {
  const base: ChatMessage = { id: "a1", role: "assistant", content: "", status: "streaming", taskId: "task-park" };
  return applyToolEvent(base, {
    id: "ask-1",
    name: "ask_human",
    phase: "start",
    input: JSON.stringify({ question: "Which fruit?" }),
  });
}

describe("isParkedState", () => {
  it("reads both wire spellings of the operator-parked states, and nothing else", () => {
    for (const state of ["input-required", "TASK_STATE_INPUT_REQUIRED", "auth-required", "TASK_STATE_AUTH_REQUIRED"]) {
      expect(isParkedState(state)).toBe(true);
    }
    for (const state of ["working", "TASK_STATE_COMPLETED", "submitted", "failed", "", undefined]) {
      expect(isParkedState(state)).toBe(false);
    }
  });
});

describe("settleStreamEnd — the bubble a live stream leaves when it closes (#3956)", () => {
  it("a PARKED turn stays streaming and paused, its ask_human card waiting — not done ✓", () => {
    const settled = settleStreamEnd(parkedLive(), { parked: true });
    expect(settled.status).toBe("streaming");
    expect(settled.paused).toBe(true);
    const card = settled.toolCalls?.[0];
    expect(card?.name).toBe("ask_human");
    expect(card?.status).toBe("running");
    expect(card?.paused).toBe(true);
    expect(card?.durationMs).toBeUndefined();
  });

  it("a parked bubble is still the one a reload reattaches to its OWN task", () => {
    const user: ChatMessage = { id: "u1", role: "user", content: "pick a fruit", status: "done" };
    const parked = [user, settleStreamEnd(parkedLive(), { parked: true })];
    expect(reattachKeyForMessages(parked)).toBe("a1:task-park");
    expect(shouldReattach(parked[1], "s-park")).toBe(true);
    // The old close — settled done — left nothing to reattach: the form was lost on reload.
    const settled = [user, settleStreamEnd(parkedLive(), { parked: false })];
    expect(reattachKeyForMessages(settled)).toBe("");
  });

  it("the live park has the SAME shape cold hydration gives that turn (#3946)", () => {
    const durable: DurableChatTurn = {
      task_id: "task-park",
      state: "TASK_STATE_INPUT_REQUIRED",
      last_updated: "2026-09-30T12:00:00Z",
      text: "",
      status: { state: "TASK_STATE_INPUT_REQUIRED" },
      artifacts: [],
      history: [
        { role: "ROLE_USER", parts: [{ text: "pick a fruit" }] },
        {
          role: "ROLE_AGENT",
          parts: [],
          metadata: { [TOOL]: { toolCallId: "ask-1", name: "ask_human", phase: "started", args: "{}" } },
        },
      ],
    };
    const rebuilt = messagesFromDurableTurn(durable);
    const hydrated = rebuilt[rebuilt.length - 1];
    const live = settleStreamEnd(parkedLive(), { parked: true });
    expect(live.status).toBe(hydrated.status);
    expect(live.paused).toBe(hydrated.paused);
    expect(live.toolCalls?.map((c) => [c.name, c.status, c.paused])).toEqual(
      hydrated.toolCalls?.map((c) => [c.name, c.status, c.paused]),
    );
  });

  it("the answer that continues the parked task settles it (form answer) or resumes it (approval)", () => {
    const parked = settleStreamEnd(parkedLive(), { parked: true });
    const [answered] = settleAnsweredPause([parked], "a1");
    expect(answered.status).toBe("done");
    expect(answered.paused).toBeUndefined();
    expect(answered.toolCalls?.[0]).toMatchObject({ status: "done", paused: undefined });
    const resumed = unpauseBubble(parked);
    expect(resumed.paused).toBeUndefined();
    expect(resumed.toolCalls?.[0]).toMatchObject({ status: "running", paused: undefined });
  });

  it("leaves an already-settled bubble alone when parked (a Stop or failure got there first)", () => {
    const stopped: ChatMessage = { ...parkedLive(), status: "error" };
    expect(settleStreamEnd(stopped, { parked: true })).toBe(stopped);
  });

  it("any other close settles done and flips a card whose end raced the close, stamping its time", () => {
    const live = parkedLive();
    const startedAt = live.toolCalls![0].startedAt!;
    const settled = settleStreamEnd(live, { parked: false, now: startedAt + 250 });
    expect(settled.status).toBe("done");
    expect(settled.paused).toBeUndefined();
    expect(settled.toolCalls?.[0]).toMatchObject({ status: "done", durationMs: 250 });
  });

  it("a done close clears a pause the bubble or its cards still carried", () => {
    const parked = settleStreamEnd(parkedLive(), { parked: true });
    const settled = settleStreamEnd(parked, { parked: false });
    expect(settled.status).toBe("done");
    expect(settled.paused).toBeUndefined();
    expect(settled.toolCalls?.[0].paused).toBeUndefined();
  });
});

describe("createParkTracker over the real frame dispatcher (#3956)", () => {
  const SESSION = "s-park";
  const HITL = "application/vnd.protolabs.hitl-v1+json";
  /** An input-required status frame carrying this hitl-v1 payload. */
  const parkFrame = (data: Record<string, unknown>) => ({
    jsonrpc: "2.0",
    id: "1",
    result: {
      statusUpdate: {
        taskId: "t1",
        contextId: SESSION,
        status: {
          state: "TASK_STATE_INPUT_REQUIRED",
          message: { parts: [{ data, metadata: { mimeType: HITL } }] },
        },
      },
    },
  });
  const workingFrame = {
    jsonrpc: "2.0",
    id: "1",
    result: { statusUpdate: { taskId: "t1", contextId: SESSION, status: { state: "TASK_STATE_WORKING" } } },
  };

  /** A tracker wired exactly as the slot wires it: payload first, then state. */
  function wired() {
    const park = createParkTracker();
    const order: string[] = [];
    const transitions: (string | null)[] = [];
    const dispatch = makeA2ADispatcher(SESSION, {
      onInputRequired: (payload) => {
        order.push("inputRequired");
        park.inputRequired(payload);
      },
      onTaskState: (state) => {
        order.push("taskState");
        transitions.push(park.taskState(state));
      },
    });
    return { park, order, transitions, dispatch: (frame: unknown) => dispatch(frame as never) };
  }

  it("the dispatcher reports the payload BEFORE the state — the plugin-form exclusion depends on it", () => {
    const { order, dispatch } = wired();
    dispatch(parkFrame({ question: "Which fruit?" }));
    expect(order).toEqual(["inputRequired", "taskState"]);
  });

  it("a plugin composer form never parks the turn; an agent question does", () => {
    const form = wired();
    form.dispatch(parkFrame({ question: "Plugin form?", plugin_callback_id: "cb-1" }));
    expect(form.park.parked).toBe(false);
    expect(form.transitions).toEqual([null]);

    const ask = wired();
    ask.dispatch(parkFrame({ question: "Which fruit?" }));
    expect(ask.park.parked).toBe(true);
    expect(ask.transitions).toEqual(["parked"]);
  });

  it("a working state after a park un-parks (the latest state wins)", () => {
    const { park, transitions, dispatch } = wired();
    dispatch(parkFrame({ question: "Which fruit?" }));
    dispatch(workingFrame);
    expect(park.parked).toBe(false);
    expect(transitions).toEqual(["parked", "unparked"]);
  });
});
