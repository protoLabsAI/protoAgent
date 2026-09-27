// ADR 0114 D2 — PaletteChat's load barrier. Nothing that writes the thread (the save
// effect, the unmount flush, the self-heal, the `initial` auto-send) runs before the
// thread has loaded, and its contextId is never replaced by a freshly minted one
// because a read is unfinished, failed or empty.
import { act, createElement as h, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@protolabsai/ui/ai", () => ({
  Conversation: ({ children }: { children: ReactNode }) => h("div", { "data-testid": "convo" }, children),
  Message: ({ children }: { children: ReactNode }) => h("div", null, children),
  PromptInput: ({ disabled }: { disabled?: boolean }) =>
    h("textarea", { "data-testid": "composer", disabled: Boolean(disabled) }),
}));
vi.mock("../chat/ChatMessageView", () => ({
  ChatMessageView: ({ message }: { message: { content: string } }) => h("div", { "data-testid": "msg" }, message.content),
}));
vi.mock("./ErrorBoundary", () => ({
  PanelSkeleton: ({ label }: { label?: string }) => h("div", { "data-testid": "skeleton" }, label),
}));

const apiMocks = vi.hoisted(() => ({
  streamChat: vi.fn(async (..._args: unknown[]) => {}),
  getTask: vi.fn(async () => ({ state: "completed", text: "done" })),
  deleteChatSession: vi.fn(async () => ({})),
}));
vi.mock("../lib/api", () => ({ api: apiMocks }));

import { PaletteChat } from "./PaletteChat";
import { PALETTE_LOAD_TIMEOUT_MS, setPaletteThreadLoader, type PaletteThread } from "./paletteChatStore";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const KEY = "protoagent.palette.chat";
const THREAD: PaletteThread = {
  contextId: "palette-keep",
  messages: [
    { role: "user", content: "earlier question" },
    { role: "assistant", content: "earlier answer", status: "done" },
  ],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

let host: HTMLDivElement;
let root: Root | null = null;
let paletteWrites: string[];

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem(KEY, JSON.stringify(THREAD));
  paletteWrites = [];
  const setItem = Storage.prototype.setItem;
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
    if (key.startsWith(KEY)) paletteWrites.push(value);
    return setItem.call(this, key, value);
  });
  apiMocks.streamChat.mockClear();
  apiMocks.getTask.mockClear();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  host.remove();
  setPaletteThreadLoader(null);
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function render(props: { initial?: string } = {}) {
  act(() => root?.render(h(PaletteChat, { agentName: "Ava", ...props })));
}

const composer = () => host.querySelector<HTMLTextAreaElement>("[data-testid=composer]")!;

describe("PaletteChat load barrier", () => {
  it("does not save, auto-send or accept input before the thread has loaded", async () => {
    const read = deferred<PaletteThread | null>();
    setPaletteThreadLoader(() => read.promise);
    render({ initial: "hello there" });

    expect(host.querySelector("[data-testid=skeleton]")).not.toBeNull();
    expect(composer().disabled).toBe(true);
    await act(async () => {});
    expect(apiMocks.streamChat).not.toHaveBeenCalled();
    expect(paletteWrites).toEqual([]);

    // Closing the palette while the read is pending must not flush an empty transcript.
    act(() => root?.unmount());
    root = null;
    expect(paletteWrites).toEqual([]);
    expect(JSON.parse(window.localStorage.getItem(KEY)!)).toEqual(THREAD);
  });

  it("once loaded, auto-sends into the STORED contextId", async () => {
    const read = deferred<PaletteThread | null>();
    setPaletteThreadLoader(() => read.promise);
    render({ initial: "hello there" });
    await act(async () => read.resolve(THREAD));

    expect(host.querySelectorAll("[data-testid=msg]")[0]?.textContent).toBe("earlier question");
    expect(composer().disabled).toBe(false);
    expect(apiMocks.streamChat).toHaveBeenCalledTimes(1);
    expect(apiMocks.streamChat.mock.calls[0]).toEqual(
      expect.arrayContaining(["hello there", "palette-keep"]),
    );
    act(() => root?.unmount()); // the close flush writes the loaded thread at once
    root = null;
    const saved = JSON.parse(paletteWrites[paletteWrites.length - 1]) as PaletteThread;
    expect(saved.contextId).toBe("palette-keep");
    expect(saved.messages.slice(0, 2)).toEqual(THREAD.messages);
  });

  it("an empty read keeps the stored contextId instead of minting a new one", async () => {
    const read = deferred<PaletteThread | null>();
    setPaletteThreadLoader(() => read.promise);
    render({ initial: "hi" });
    await act(async () => read.resolve(null)); // the transcript read found nothing
    expect(apiMocks.streamChat.mock.calls[0]?.[1]).toBe("palette-keep");
    for (const raw of paletteWrites) expect((JSON.parse(raw) as PaletteThread).contextId).toBe("palette-keep");
  });

  it("a timed-out read is failed — no save, no send — and a late success still loads it", async () => {
    vi.useFakeTimers();
    const read = deferred<PaletteThread | null>();
    setPaletteThreadLoader(() => read.promise);
    render({ initial: "late hello" });

    act(() => vi.advanceTimersByTime(PALETTE_LOAD_TIMEOUT_MS));
    expect(host.querySelector("[data-testid=palette-chat-load-failed]")).not.toBeNull();
    expect(composer().disabled).toBe(true);
    expect(apiMocks.streamChat).not.toHaveBeenCalled();
    expect(paletteWrites).toEqual([]);

    await act(async () => read.resolve(THREAD));
    expect(host.querySelector("[data-testid=palette-chat-load-failed]")).toBeNull();
    expect(apiMocks.streamChat).toHaveBeenCalledTimes(1);
    expect(apiMocks.streamChat.mock.calls[0]?.[1]).toBe("palette-keep");
  });

  it("a rejected read never self-heals or writes", async () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify({
        contextId: "palette-keep",
        messages: [{ role: "assistant", content: "partial", status: "streaming", taskId: "task-9" }],
      }),
    );
    paletteWrites = [];
    const read = deferred<PaletteThread | null>();
    setPaletteThreadLoader(() => read.promise);
    render();
    await act(async () => read.reject(new Error("idb closed")));
    expect(host.querySelector("[data-testid=palette-chat-load-failed]")).not.toBeNull();
    expect(apiMocks.getTask).not.toHaveBeenCalled(); // no self-heal against an unread thread
    act(() => root?.unmount());
    root = null;
    expect(paletteWrites).toEqual([]);
  });

  it("the default synchronous read loads on the first render (no skeleton flash)", () => {
    render();
    expect(host.querySelector("[data-testid=skeleton]")).toBeNull();
    expect(host.querySelectorAll("[data-testid=msg]")).toHaveLength(2);
    expect(composer().disabled).toBe(false);
  });
});
