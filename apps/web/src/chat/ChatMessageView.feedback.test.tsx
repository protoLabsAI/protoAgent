import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { feedbackApi, type FeedbackTurn } from "../lib/api/feedback";
import type { ChatMessage } from "../lib/types";
import { ChatMessageView } from "./ChatMessageView";
import { useSessionFeedback } from "./useSessionFeedback";

vi.mock("../lib/api/feedback", () => ({ feedbackApi: { get: vi.fn(), set: vi.fn() } }));
const messages: ChatMessage[] = [1, 2].map((n) => ({ id: `a${n}`, taskId: `t${n}`, role: "assistant", status: "done", content: `Answer ${n}` }));
const turns: FeedbackTurn[] = [1, 2].map((n) => ({ task_id: `t${n}`, trace_id: `trace${n}`, created_at: "", rating: null, note: null }));
let root: Root;
let el: HTMLDivElement;
let client: QueryClient;
function Harness({ incognito = false, items = messages }: { incognito?: boolean; items?: ChatMessage[] }) {
  const feedback = useSessionFeedback("session", items, incognito);
  return <>{feedback.error && <p role="alert">{feedback.error}</p>}{items.map((message) =>
    <ChatMessageView key={message.id} message={message} actions={{ sessionId: "session", incognito, feedback }} />)}</>;
}
async function mount(incognito = false, items = messages) {
  await act(async () => root.render(<QueryClientProvider client={client}><Harness incognito={incognito} items={items} /></QueryClientProvider>));
}
function button(name: string) { return el.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`)!; }
async function click(name: string) { await act(async () => button(name).click()); }
beforeEach(() => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  vi.mocked(feedbackApi.get).mockResolvedValue({ enabled: true, tracing_enabled: true, turns });
  vi.mocked(feedbackApi.set).mockImplementation(async (body) => ({ ok: true, rating: body.value, note: body.note }));
  el = document.createElement("div"); document.body.append(el); root = createRoot(el);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => { act(() => root.unmount()); el.remove(); client.clear(); });
it("loads once for multiple messages, rates, changes and un-rates", async () => {
  await mount();
  expect(feedbackApi.get).toHaveBeenCalledTimes(1);
  await click("Thumbs up");
  expect(button("Thumbs up").getAttribute("aria-pressed")).toBe("true");
  expect(feedbackApi.set).toHaveBeenLastCalledWith({ session_id: "session", trace_id: "trace1", value: 1, note: "", incognito: false });
  await click("Thumbs down");
  expect(button("Thumbs down").getAttribute("aria-pressed")).toBe("true");
  expect(el.querySelector("input")).not.toBeNull();
  await click("Thumbs down");
  expect(feedbackApi.set).toHaveBeenLastCalledWith(expect.objectContaining({ value: null }));
  expect(button("Thumbs down").getAttribute("aria-pressed")).toBe("false");
  await click("Thumbs up"); await click("Thumbs up");
  expect(feedbackApi.set).toHaveBeenLastCalledWith(expect.objectContaining({ value: null }));
});
it("saves an optional one-line note and restores a saved rating", async () => {
  await mount(); await click("Thumbs down");
  const input = el.querySelector("input")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Needs sources");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => el.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(feedbackApi.set).toHaveBeenLastCalledWith(expect.objectContaining({ value: 0, note: "Needs sources" }));
  vi.mocked(feedbackApi.get).mockResolvedValue({ enabled: true, tracing_enabled: true, turns: [{ ...turns[0], rating: 0, note: "Needs sources" }] });
  await act(async () => root.render(null)); await mount();
  expect(button("Thumbs down").getAttribute("aria-pressed")).toBe("true");
  expect(el.textContent).toContain("Needs sources");
});
it("rolls back an optimistic save and shows a non-blocking error", async () => {
  let reject!: (reason: Error) => void;
  vi.mocked(feedbackApi.set).mockReturnValue(new Promise((_resolve, fail) => { reject = fail; }));
  await mount(); await click("Thumbs up");
  expect(button("Thumbs up").getAttribute("aria-pressed")).toBe("true");
  expect(button("Thumbs up").disabled).toBe(true);
  await act(async () => reject(new Error("offline")));
  expect(button("Thumbs up").getAttribute("aria-pressed")).toBe("false");
  expect(el.querySelector('[role="alert"]')?.textContent).toContain("Could not save");
});
it("hides incognito and unresolved turns, but rates tracing-off rows", async () => {
  await mount(true); expect(feedbackApi.get).not.toHaveBeenCalled(); expect(button("Thumbs up")).toBeNull();
  await mount(false, [{ ...messages[0], taskId: "missing" }]); expect(button("Thumbs up")).toBeNull();
  vi.mocked(feedbackApi.get).mockResolvedValue({ enabled: true, tracing_enabled: false, turns: [{ ...turns[0], trace_id: null }] });
  await act(async () => root.render(null));
  await mount(false, [messages[0]]); await click("Thumbs up");
  expect(feedbackApi.set).toHaveBeenLastCalledWith(expect.objectContaining({ trace_id: "", value: 1 }));
});
it("shows read failures without preventing the answer from rendering", async () => {
  vi.mocked(feedbackApi.get).mockRejectedValue(new Error("offline")); await mount();
  expect(el.querySelector('[role="alert"]')?.textContent).toContain("Could not load");
  expect(el.textContent).toContain("Answer 1");
});
it("uses ordered rows only for a complete alignment and rejects ambiguous task matches", async () => {
  await mount(false, messages.map(({ taskId: _taskId, ...message }) => message));
  await click("Thumbs up");
  expect(feedbackApi.set).toHaveBeenLastCalledWith(expect.objectContaining({ trace_id: "trace1" }));
  await act(async () => root.render(null));
  vi.mocked(feedbackApi.get).mockResolvedValue({ enabled: true, tracing_enabled: true, turns: [turns[0], { ...turns[1], task_id: "t1" }] });
  await mount();
  expect(button("Thumbs up")).toBeNull();
});
it("keeps tracing-off rows synchronized with the backend's shared local key", async () => {
  vi.mocked(feedbackApi.get).mockResolvedValue({ enabled: true, tracing_enabled: false, turns: turns.map((turn) => ({ ...turn, trace_id: null })) });
  await mount(); await click("Thumbs up");
  expect([...el.querySelectorAll('button[aria-label="Thumbs up"]')].map((node) => node.getAttribute("aria-pressed"))).toEqual(["true", "true"]);
});
it("ignores a session read that finishes after switching to incognito", async () => {
  let resolve!: (value: Awaited<ReturnType<typeof feedbackApi.get>>) => void;
  vi.mocked(feedbackApi.get).mockReturnValue(new Promise((done) => { resolve = done; }));
  await mount(); await mount(true);
  await act(async () => resolve({ enabled: true, tracing_enabled: true, turns }));
  expect(button("Thumbs up")).toBeNull();
  expect(feedbackApi.set).not.toHaveBeenCalled();
});
it("waits for an in-flight save before refreshing newly settled turns", async () => {
  let resolve!: (value: Awaited<ReturnType<typeof feedbackApi.set>>) => void;
  vi.mocked(feedbackApi.set).mockReturnValue(new Promise((done) => { resolve = done; }));
  await mount(); await click("Thumbs up");
  vi.mocked(feedbackApi.get).mockResolvedValue({ enabled: true, tracing_enabled: true, turns: [{ ...turns[0], rating: 1 }, turns[1]] });
  await mount(false, [...messages, { id: "a3", taskId: "t3", role: "assistant", content: "New answer", status: "done" }]);
  expect(feedbackApi.get).toHaveBeenCalledTimes(1);
  await act(async () => resolve({ ok: true, rating: 1, note: "" }));
  expect(feedbackApi.get).toHaveBeenCalledTimes(2);
  expect(button("Thumbs up").getAttribute("aria-pressed")).toBe("true");
});
