import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PromptInput } from "@protolabsai/ui/ai";
import { api } from "../lib/api";
import { chatStore, registerGoalKickoff } from "./chat-store";
import { ChatSessionSlot } from "./ChatSessionSlot";
import { ChatTranscript } from "./ChatTranscript";
import { loadDraftContext } from "./scratchState";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const view = vi.hoisted(() => ({
  composer: null as ComponentProps<typeof PromptInput> | null,
  transcript: null as ComponentProps<typeof ChatTranscript> | null,
}));
vi.mock("@protolabsai/ui/ai", async (original) => ({
  ...await original<typeof import("@protolabsai/ui/ai")>(),
  PromptInput: (props: ComponentProps<typeof PromptInput>) => {
    view.composer = props;
    return <textarea readOnly value={props.value} />;
  },
}));
vi.mock("./ChatTranscript", () => ({
  ChatTranscript: (props: ComponentProps<typeof ChatTranscript>) => {
    view.transcript = props;
    return null;
  },
}));
vi.mock("@tanstack/react-query", async (original) => ({
  ...await original<typeof import("@tanstack/react-query")>(),
  useQuery: () => ({ data: undefined }),
}));
vi.mock("./ComposerModelSelect", () => ({ ComposerModelSelect: () => null }));
vi.mock("./ChatStatusRail", () => ({ ChatStatusRail: () => null }));
vi.mock("./LocalTurnControls", () => ({ LocalTurnControls: () => null }));
vi.mock("./GoalRunStrip", () => ({ GoalRunStrip: () => null }));
vi.mock("./DirectiveStrip", () => ({ DirectiveWorkRequest: () => null }));
vi.mock("./sessionListSync", () => ({ registerSessionOnSend: vi.fn() }));

let host: HTMLDivElement;
let root: Root;
let sessionId: string;
beforeEach(async () => {
  sessionStorage.clear();
  vi.spyOn(api, "rewindChatSession").mockResolvedValue({ found: true } as Awaited<ReturnType<typeof api.rewindChatSession>>);
  const session = chatStore.continueConversation({ contextId: "ctx-original", title: "Original", origin: "From Claude Code" });
  sessionId = session.id;
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(
    <ChatSessionSlot sessionId={sessionId} visible surfaceActive={false} onError={vi.fn()} />,
  ));
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  sessionStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
});
async function send(text: string) {
  await act(async () => view.composer!.onChange!(text));
  await act(async () => view.composer!.onSubmit!());
}

it.each(["failed", "failed-then-done", "throw"] as const)(
  "keeps hidden continuation context through %s and consumes it only after a successful retry",
  async (failure) => {
    expect(host.querySelector("textarea")!.value).not.toContain("<!--");
    const reference = loadDraftContext(sessionId);
    expect(reference).toContain('recall_session(session_id="ctx-original")');
    const stream = vi.spyOn(api, "streamChat").mockImplementationOnce(async (_text, _session, callbacks) => {
      if (failure === "throw") throw new Error("offline");
      callbacks?.onFailed?.("model failed");
      if (failure === "failed-then-done") callbacks?.onDone?.();
    }).mockImplementation(async (_text, _session, callbacks) => {
      callbacks?.onText?.("Answer", false);
      callbacks?.onDone?.();
    });
    await send("What happened?");
    expect(stream.mock.calls[0][0]).toBe(`What happened?\n\n${reference}`);
    expect(stream.mock.calls[0][3]?.display).toBe("What happened?");
    expect(loadDraftContext(sessionId)).toBe(reference);
    await send("Try again");
    expect(stream.mock.calls[1][0]).toBe(`Try again\n\n${reference}`);
    expect(loadDraftContext(sessionId)).toBe("");
    const messages = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)!.messages;
    expect(messages.filter((m) => m.role === "user").every((m) => !m.content.includes("<!--"))).toBe(true);

    // Rewind discards the server's original turn: regeneration must restore its reference.
    const assistant = messages[messages.length - 1];
    await act(async () => view.transcript!.actions.onRegenerate!(assistant.id!));
    expect(api.rewindChatSession).toHaveBeenCalled();
    expect(stream.mock.calls[2][0]).toBe(`Try again\n\n${reference}`);
    await send("Next question");
    expect(stream.mock.calls[3][0]).toBe("Next question");
  },
);

it("leaves continuation context for the composer when an automatic kickoff sends first", async () => {
  vi.useFakeTimers();
  const reference = loadDraftContext(sessionId);
  const stream = vi.spyOn(api, "streamChat").mockImplementation(async (_text, _session, callbacks) => {
    callbacks?.onDone?.();
  });
  await act(async () => {
    registerGoalKickoff(sessionId, "Start the goal");
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(stream.mock.calls[0][0]).toBe("Start the goal");
  expect(loadDraftContext(sessionId)).toBe(reference);
  await send("Continue the conversation");
  expect(stream.mock.calls[1][0]).toBe(`Continue the conversation\n\n${reference}`);
});
