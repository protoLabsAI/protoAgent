// The composer answers the open decision in its own chat (#5668 §4): no "Reply in chat"
// button, the placeholder is the only visible change, and approval gates keep today's hold.
import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PromptInput } from "@protolabsai/ui/ai";
import { registerSlashCommand } from "../ext/slashRegistry";
import { api } from "../lib/api";
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

const slashRuns = vi.fn((_ctx: { rest: string }) => true);
registerSlashCommand({ name: "decisiontest", description: "test-only client command", run: slashRuns });

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
  slashRuns.mockClear();
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
const queued = () => chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)!;

it("answers the open decision straight from the composer and restores the normal placeholder", async () => {
  expect(view.composer!.placeholder).toBe("Message Gina…");
  expect(hint()).toBeNull();
  expect(field().hasAttribute("aria-describedby")).toBe(false);

  await parkOn(decision);
  // The placeholder is the only visible change; the hidden line is the screen-reader copy of it.
  expect(view.composer!.placeholder).toBe("Type your answer…");
  expect(hint()!.textContent).toBe("Your message answers Gina's question.");
  expect(field().getAttribute("aria-describedby")).toBe(hint()!.id);
  // The matching transcript card owns the pause: no generic form floats over the composer.
  expect(host.querySelector(".hitl-float form, .hitl-float button")).toBeNull();

  await send("  Hold it for review  ");
  // Resumed with the typed text — not queued behind the form as a held steer.
  expect(steer).not.toHaveBeenCalled();
  expect(stream).toHaveBeenCalledTimes(1);
  expect(stream.mock.calls[0][0]).toBe("Hold it for review");
  expect(stream.mock.calls[0][3]?.hitlResume).toBe(true);
  expect(view.composer!.value).toBe("");

  // Answered: the placeholder and the hidden description go back with it.
  expect(view.composer!.placeholder).toBe("Message Gina…");
  expect(hint()).toBeNull();
  expect(field().hasAttribute("aria-describedby")).toBe(false);
});

it("still holds typed text behind an approval gate, and never resumes it", async () => {
  await parkOn(approval);
  expect(view.composer!.placeholder).toBe("Message Gina…");
  expect(hint()).toBeNull();
  expect(field().hasAttribute("aria-describedby")).toBe(false);

  await send("approved");
  expect(stream).not.toHaveBeenCalled();
  expect(steer).toHaveBeenCalledTimes(1);
  expect(steer.mock.calls[0][2]).toBe("approved");
  // The gate is still open, waiting for its own buttons.
  expect([...host.querySelectorAll(".hitl-float button")].map((b) => b.textContent)).toContain("Approve");
});

it("holds typed text for a decision whose card is not in this chat's transcript", async () => {
  // An id-less decision has no durable card: the generic form keeps the pause.
  await parkOn({ kind: "decision", question: "Ship?" });
  expect(view.composer!.placeholder).toBe("Message Gina…");
  expect(hint()).toBeNull();
  await send("Hold it");
  expect(stream).not.toHaveBeenCalled();
  expect(steer).toHaveBeenCalledTimes(1);
});

it("runs a leading-slash message as a slash command, never as the answer", async () => {
  await parkOn(decision);
  await send("/decisiontest now");
  expect(slashRuns).toHaveBeenCalledTimes(1);
  expect(slashRuns.mock.calls[0][0]).toMatchObject({ rest: "now" });
  expect(stream).not.toHaveBeenCalled();
  expect(steer).not.toHaveBeenCalled();
  expect(view.composer!.value).toBe("");
  // The decision is still open and still answerable.
  expect(view.composer!.placeholder).toBe("Type your answer…");

  // A slash message that is not a client command keeps today's hold: held, not an answer.
  await send("/not-a-client-command");
  expect(stream).not.toHaveBeenCalled();
  expect(steer).toHaveBeenCalledTimes(1);
  expect(steer.mock.calls[0][2]).toBe("/not-a-client-command");
  expect(view.composer!.placeholder).toBe("Type your answer…");

  await send("Ship it tomorrow");
  expect(stream).toHaveBeenCalledTimes(1);
  expect(stream.mock.calls[0][0]).toBe("Ship it tomorrow");
  expect(queued().messages.some((m) => m.content === "Ship it tomorrow")).toBe(true);
});
