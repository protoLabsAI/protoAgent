import { act, createElement as h, StrictMode, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type UpdateInfo = { version: string; current: string; notes: string };

const mocks = vi.hoisted(() => ({
  checkUpdate: vi.fn<() => Promise<UpdateInfo | null>>(),
  installUpdate: vi.fn(),
  launchUpdateResult: vi.fn(),
  consumeUpdateRequest: vi.fn(),
  ackUpdateRequest: vi.fn(),
  listen: vi.fn(),
  toast: vi.fn(),
  primary: true,
  eventHandler: null as ((requestId: number) => void) | null,
}));

vi.mock("../lib/api", () => ({
  api: {
    checkUpdate: mocks.checkUpdate,
    installUpdate: mocks.installUpdate,
    launchUpdateResult: mocks.launchUpdateResult,
    consumeUpdateRequest: mocks.consumeUpdateRequest,
    ackUpdateRequest: mocks.ackUpdateRequest,
  },
  isDesktopWebview: () => true,
}));

vi.mock("../lib/desktop", () => ({
  isPrimaryDesktopWindow: () => mocks.primary,
  listen: mocks.listen,
}));

vi.mock("@protolabsai/ui/primitives", () => ({
  Button: ({ children, disabled, onClick }: { children?: ReactNode; disabled?: boolean; onClick?: () => void }) =>
    h("button", { disabled, onClick }, children),
}));

vi.mock("@protolabsai/ui/overlays", () => ({
  Dialog: ({ open, title, footer, children }: { open: boolean; title?: ReactNode; footer?: ReactNode; children?: ReactNode }) =>
    open ? h("section", { "data-testid": "update-dialog" }, title, children, footer) : null,
  useToast: () => mocks.toast,
}));

vi.mock("../chat/LazyMarkdown", () => ({ Markdown: ({ children }: { children?: ReactNode }) => h("div", null, children) }));

import { UpdateNotice } from "./UpdateNotice";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

const updateA: UpdateInfo = { version: "0.141.0", current: "0.140.0", notes: "Release A" };
const updateB: UpdateInfo = { version: "0.142.0", current: "0.140.0", notes: "Release B" };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function mountNotice() {
  await act(async () => {
    root.render(h(UpdateNotice));
  });
  await vi.waitFor(() =>
    expect(mocks.listen).toHaveBeenCalledWith("updater:check-requested", expect.any(Function), {
      target: { kind: "WebviewWindow", label: "main" },
    }),
  );
}

function buttonByText(text: string) {
  return [...document.body.querySelectorAll("button")].find((button) => button.textContent?.trim() === text);
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  mocks.eventHandler = null;
  mocks.primary = true;
  mocks.checkUpdate.mockReset();
  mocks.installUpdate.mockReset();
  mocks.launchUpdateResult.mockReset().mockResolvedValue({ done: true, update: null });
  mocks.consumeUpdateRequest.mockReset().mockResolvedValue(null);
  mocks.ackUpdateRequest.mockReset().mockResolvedValue(undefined);
  mocks.toast.mockReset();
  mocks.listen.mockReset().mockImplementation(async (_event: string, handler: (requestId: number) => void) => {
    mocks.eventHandler = handler;
    return () => {};
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe("UpdateNotice tray requests", () => {
  it("does not subscribe or check from a secondary desktop window", async () => {
    mocks.primary = false;

    await act(async () => root.render(h(UpdateNotice)));

    expect(mocks.listen).not.toHaveBeenCalled();
    expect(mocks.launchUpdateResult).not.toHaveBeenCalled();
    expect(mocks.checkUpdate).not.toHaveBeenCalled();
  });

  it("replays a request captured by Rust before the listener mounted", async () => {
    mocks.consumeUpdateRequest.mockResolvedValue(7);
    mocks.checkUpdate.mockResolvedValue(updateA);

    await mountNotice();

    await vi.waitFor(() => expect(mocks.checkUpdate).toHaveBeenCalledTimes(1));
    expect(mocks.ackUpdateRequest).toHaveBeenCalledWith(7);
    expect(document.body.textContent).toContain("0.141.0");
    expect(document.querySelector('[data-testid="update-dialog"]')).not.toBeNull();
  });

  it("deduplicates a live event against the same durable pending pull", async () => {
    const pending = deferred<number | null>();
    mocks.consumeUpdateRequest.mockReturnValue(pending.promise);
    mocks.checkUpdate.mockResolvedValue(updateA);
    await mountNotice();

    await act(async () => mocks.eventHandler?.(7));
    await act(async () => pending.resolve(7));

    expect(mocks.checkUpdate).toHaveBeenCalledTimes(1);
    expect(mocks.ackUpdateRequest).toHaveBeenCalledTimes(1);
    expect(document.querySelector('[data-testid="update-dialog"]')).not.toBeNull();
  });

  it("coalesces repeated tray clicks into one updater check and one current-version toast", async () => {
    const check = deferred<UpdateInfo | null>();
    mocks.checkUpdate.mockReturnValue(check.promise);
    await mountNotice();

    act(() => {
      mocks.eventHandler?.(1);
      mocks.eventHandler?.(2);
    });
    expect(mocks.checkUpdate).toHaveBeenCalledTimes(1);

    await act(async () => check.resolve(null));
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    expect(mocks.toast).toHaveBeenCalledWith(expect.objectContaining({ tone: "success", title: "You're up to date" }));
  });

  it("keeps ambient errors quiet but surfaces the same error for an interactive request", async () => {
    vi.useFakeTimers();
    mocks.checkUpdate.mockRejectedValue(new Error("manifest unavailable"));
    await act(async () => root.render(h(UpdateNotice)));

    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(mocks.checkUpdate).toHaveBeenCalledTimes(1);
    expect(mocks.toast).not.toHaveBeenCalled();

    await act(async () => mocks.eventHandler?.(1));
    expect(mocks.checkUpdate).toHaveBeenCalledTimes(2);
    expect(mocks.toast).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "error", title: "Couldn't check for updates", message: "manifest unavailable" }),
    );
  });

  it("re-presents a superseding release and requires a second confirmation", async () => {
    mocks.checkUpdate.mockResolvedValue(updateA);
    mocks.installUpdate.mockResolvedValue({ status: "superseded", update: updateB });
    await mountNotice();
    await act(async () => mocks.eventHandler?.(1));

    await act(async () => buttonByText("Update & Restart")?.click());

    expect(mocks.installUpdate).toHaveBeenCalledWith("0.141.0", expect.any(Function));
    expect(document.body.textContent).toContain("0.142.0");
    expect(document.body.textContent).toContain("Release B");
    expect(buttonByText("Update & Restart")).toBeDefined();
    expect(mocks.toast).toHaveBeenCalledWith(
      expect.objectContaining({ tone: "info", title: "A newer update is now available" }),
    );
  });
});

describe("UpdateNotice launch and ambient ownership", () => {
  it("auto-opens an update found by the launch check", async () => {
    mocks.launchUpdateResult.mockResolvedValue({ done: true, update: updateA });

    await mountNotice();

    await vi.waitFor(() => expect(document.querySelector('[data-testid="update-dialog"]')).not.toBeNull());
    expect(document.body.textContent).toContain("Release A");
    expect(mocks.checkUpdate).not.toHaveBeenCalled();
  });

  it("waits past the 10s ambient timer for a slow launch check and still auto-opens it", async () => {
    vi.useFakeTimers();
    let launchDone = false;
    mocks.launchUpdateResult.mockImplementation(async () =>
      launchDone ? { done: true, update: updateA } : { done: false, update: null },
    );
    mocks.checkUpdate.mockResolvedValue(null);
    await act(async () => root.render(h(StrictMode, null, h(UpdateNotice))));

    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(mocks.checkUpdate).not.toHaveBeenCalled();
    expect(document.querySelector('[data-testid="update-dialog"]')).toBeNull();

    launchDone = true;
    await act(async () => vi.advanceTimersByTimeAsync(1_000));

    expect(document.querySelector('[data-testid="update-dialog"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Release A");
    expect(mocks.checkUpdate).not.toHaveBeenCalled();
  });

  it("releases ambient polling after the bounded launch wait expires", async () => {
    vi.useFakeTimers();
    mocks.launchUpdateResult.mockResolvedValue({ done: false, update: null });
    mocks.checkUpdate.mockResolvedValue(null);
    await act(async () => root.render(h(UpdateNotice)));

    await act(async () => vi.advanceTimersByTimeAsync(19_000));
    expect(mocks.checkUpdate).not.toHaveBeenCalled();

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(mocks.checkUpdate).toHaveBeenCalledTimes(1);
    expect(mocks.toast).not.toHaveBeenCalled();
  });
});
