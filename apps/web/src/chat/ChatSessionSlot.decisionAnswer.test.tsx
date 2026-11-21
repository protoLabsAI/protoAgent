// A typed message never answers the open decision just because one is pending (#5678; the
// unreleased #5675 behaviour is withdrawn): it is held like any other message typed during a
// pause, the placeholder stays the normal one, and the composer carries no "answers" description.
import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PromptInput } from "@protolabsai/ui/ai";
import { api } from "../lib/api";
import { decisionsApi } from "../lib/api/decisions";
import type { HitlPayload } from "../lib/types";
import { chatStore } from "./chat-store";
import { ChatSessionSlot } from "./ChatSessionSlot";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const view = vi.hoisted(() => ({ composer: null as ComponentProps<typeof PromptInput> | null }));
vi.mock("@protolabsai/ui/ai", async (original) => ({
  ...await original<typeof import("@protolabsai/ui/ai")>(),
  // The real field's contract the slot relies on: the textarea node lands on `inputRef`.
  PromptInput: (props: ComponentProps<typeof PromptInput>) => {
    view.composer = props;
    return <textarea ref={props.inputRef} readOnly value={props.value} placeholder={props.placeholder} />;
  },
}));
vi.mock("./ChatTranscript", () => ({ ChatTranscript: () => null }));
vi.mock("@tanstack/react-query", async (original) => ({
  ...await original<typeof import("@tanstack/react-query")>(),
  useQuery: () => ({ data: { identity: { name: "Gina" } } }),
}));
vi.mock("./ComposerModelSelect", () => ({ ComposerModelSelect: () => null }));
vi.mock("./ChatStatusRail", () => ({ ChatStatusRail: () => null }));
vi.mock("./LocalTurnControls", () => ({ LocalTurnControls: () => null }));
vi.mock("./GoalRunStrip", () => ({ GoalRunStrip: () => null }));
vi.mock("./DirectiveStrip", () => ({ DirectiveWorkRequest: () => null }));
vi.mock("./sessionListSync", () => ({ registerSessionOnSend: vi.fn() }));
vi.mock("../lib/notify", () => ({ notifyIfHidden: vi.fn() }));

const decision: HitlPayload = { kind: "decision", decision_id: "dec-1", question: "Ship?" };
const approval: HitlPayload = { kind: "approval", description: "Run the deploy script", detail: "./deploy.sh" };

let host: HTMLDivElement;
let root: Root;
let sessionId: string;
let stream: ReturnType<typeof spyStream>;
let steer: ReturnType<typeof spySteer>;
const spyStream = () => vi.spyOn(api, "streamChat");
const spySteer = () => vi.spyOn(api, "steerChat");
/** What the NEXT turn parks on; null lets it finish. */
let park: HitlPayload | null;

beforeEach(async () => {
  sessionStorage.clear();
  park = null;
  stream = spyStream().mockImplementation(async (_text, _session, callbacks) => {
    const payload = park;
    park = null;
    if (payload) {
      callbacks?.onHitlPayload?.(payload);
      callbacks?.onInputRequired?.(payload);
    } else {
      callbacks?.onText?.("Done", false);
    }
    callbacks?.onDone?.();
  });
  steer = spySteer().mockResolvedValue({} as Awaited<ReturnType<typeof api.steerChat>>);
  sessionId = chatStore.continueConversation({ contextId: `ctx-${Math.random()}`, title: "Decision chat", origin: "test" }).id;
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
  vi.restoreAllMocks();
});

async function send(text: string) {
  await act(async () => view.composer!.onChange!(text));
  await act(async () => view.composer!.onSubmit!());
}
async function parkOn(payload: HitlPayload) {
  park = payload;
  await send("Start the work");
  stream.mockClear();
}
const field = () => host.querySelector("textarea")!;
const hint = () => host.querySelector<HTMLElement>(".composer-wrap .conversations-announce");

it("does not answer the open decision with a typed message", async () => {
  const answerRoute = vi.spyOn(decisionsApi, "answerDecision");
  expect(view.composer!.placeholder).toBe("Message Gina…");

  await parkOn(decision);
  // Nothing about the composer says the next message is an answer.
  expect(view.composer!.placeholder).toBe("Message Gina…");
  expect(view.composer!.placeholder).not.toBe("Type your answer…");
  expect(hint()).toBeNull();
  expect(host.textContent).not.toContain("Your message answers");
  expect(field().hasAttribute("aria-describedby")).toBe(false);

  await send("hold on, one question first");
  // Neither answer path fires: no decision answer route, no interrupt resume with the text.
  expect(answerRoute).not.toHaveBeenCalled();
  expect(stream).not.toHaveBeenCalled();
  // It is held like any message typed during a pause, and the draft is consumed.
  expect(steer).toHaveBeenCalledTimes(1);
  expect(steer.mock.calls[0][2]).toBe("hold on, one question first");
  expect(view.composer!.value).toBe("");

  // The decision is still pending, and a second message is treated the same way.
  await send("Ship it");
  expect(answerRoute).not.toHaveBeenCalled();
  expect(stream).not.toHaveBeenCalled();
  expect(steer).toHaveBeenCalledTimes(2);
  expect(view.composer!.placeholder).toBe("Message Gina…");
  expect(hint()).toBeNull();
});

it("still holds typed text behind an approval gate, and never resumes it", async () => {
  await parkOn(approval);
  expect(view.composer!.placeholder).toBe("Message Gina…");
  expect(hint()).toBeNull();

  await send("approved");
  expect(stream).not.toHaveBeenCalled();
  expect(steer).toHaveBeenCalledTimes(1);
  expect(steer.mock.calls[0][2]).toBe("approved");
  // The gate is still open, waiting for its own buttons.
  expect([...host.querySelectorAll(".hitl-float button")].map((b) => b.textContent)).toContain("Approve");
});
