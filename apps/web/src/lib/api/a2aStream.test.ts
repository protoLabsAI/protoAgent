// The shared A2A frame dispatcher's task-identity + task-state surface (#3930).
import { describe, expect, it, vi } from "vitest";

import { makeA2ADispatcher, type A2AFrame } from "./a2aStream";

const CTX = "chat-1";

describe("makeA2ADispatcher: task state and identity (#3930)", () => {
  it("reports a Task snapshot's state AFTER replaying it, so a paused form is up first", () => {
    const order: string[] = [];
    const dispatch = makeA2ADispatcher(CTX, {
      onInputRequired: () => order.push("hitl"),
      onTaskState: (state) => order.push(`state:${state}`),
    });
    dispatch({
      result: {
        task: {
          id: "t1",
          contextId: CTX,
          status: {
            state: "TASK_STATE_INPUT_REQUIRED",
            message: {
              parts: [
                {
                  data: { question: "Favourite fruit?" },
                  metadata: { mimeType: "application/vnd.protolabs.hitl-v1+json" },
                } as never,
              ],
            },
          },
        },
      },
    } as A2AFrame);
    expect(order).toEqual(["hitl", "state:TASK_STATE_INPUT_REQUIRED"]);
  });

  it("reports a status update's state", () => {
    const onTaskState = vi.fn();
    const dispatch = makeA2ADispatcher(CTX, { onTaskState });
    dispatch({ result: { statusUpdate: { taskId: "t1", contextId: CTX, status: { state: "TASK_STATE_WORKING" } } } });
    dispatch({
      result: { statusUpdate: { taskId: "t1", contextId: CTX, status: { state: "TASK_STATE_INPUT_REQUIRED" } } },
    });
    expect(onTaskState.mock.calls.map((c) => c[0])).toEqual(["TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED"]);
  });

  it("names the task off the first update when no Task frame comes (a HITL answer continuing its task)", () => {
    const onTaskId = vi.fn();
    const dispatch = makeA2ADispatcher(CTX, { onTaskId });
    dispatch({ result: { statusUpdate: { taskId: "parked-1", contextId: CTX, status: { state: "TASK_STATE_WORKING" } } } });
    dispatch({ result: { artifactUpdate: { taskId: "parked-1", contextId: CTX, artifact: { parts: [{ text: "hi" }] } } } });
    dispatch({ result: { statusUpdate: { taskId: "parked-1", contextId: CTX, status: { state: "TASK_STATE_COMPLETED" } } } });
    expect(onTaskId.mock.calls).toEqual([["parked-1"]]);
  });

  it("a live turn's Task frame names it once — its updates do not re-announce it", () => {
    const onTaskId = vi.fn();
    const dispatch = makeA2ADispatcher(CTX, { onTaskId });
    dispatch({ result: { task: { id: "t9", contextId: CTX, status: { state: "TASK_STATE_SUBMITTED" } } } });
    dispatch({ result: { statusUpdate: { taskId: "t9", contextId: CTX, status: { state: "TASK_STATE_WORKING" } } } });
    expect(onTaskId.mock.calls).toEqual([["t9"]]);
  });
});

describe("a task answered across legs (#3930)", () => {
  it("each artifact opens its own paragraph; a single-artifact turn is unchanged", async () => {
    const { joinArtifactTexts, textFromTerminalTask } = await import("./a2aStream");
    expect(joinArtifactTexts(["Let me ask.", "You like banana."])).toBe("Let me ask.\n\nYou like banana.");
    expect(joinArtifactTexts(["", "You like banana."])).toBe("You like banana.");
    expect(
      textFromTerminalTask({ artifacts: [{ parts: [{ text: "one " }, { text: "artifact" }] }] }),
    ).toBe("one artifact");
    const onText = vi.fn();
    const dispatch = makeA2ADispatcher(CTX, { onText });
    dispatch({
      result: {
        task: {
          id: "t1",
          contextId: CTX,
          status: { state: "TASK_STATE_COMPLETED" },
          artifacts: [{ parts: [{ text: "Let me ask." }] }, { parts: [{ text: "You like banana." }] }],
        } as never,
      },
    });
    expect(onText).toHaveBeenLastCalledWith("Let me ask.\n\nYou like banana.", false);
  });
});
