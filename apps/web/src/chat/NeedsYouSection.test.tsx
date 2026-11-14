import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, expect, it, vi } from "vitest";
import { NeedsYouSection } from "./NeedsYouSection";
import { openWorkGroup } from "./ChatStatusRail";
import { WorkView } from "../work/WorkView";
import type { WorkItem, WorkItems, WorkSource } from "../work/useWorkItems";
import { beginThisChatVisit, endThisChatVisit } from "../work/visitScope";
import { useUI } from "../state/uiStore";
import { api } from "../lib/api";

const data = vi.hoisted(() => ({ sources: {} as WorkItems }));
const openChat = vi.hoisted(() => vi.fn());
vi.mock("../work/useWorkItems", () => ({ useWorkItems: () => data.sources }));
vi.mock("../work/openSourceChat", () => ({ openSourceChat: openChat }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  endThisChatVisit();
  useUI.setState({ surfaceStates: {}, surfaceModal: null });
  vi.restoreAllMocks();
  openChat.mockClear();
});

it.each(["agent-handoff", undefined])("opens the shared answer detail for source %s without a chat error", async (source) => {
  const item: WorkItem = {
    kind: "task", id: "waiting", status: "needs-you", title: "Ship release", meta: "", sessionId: source,
    raw: { id: "waiting", title: "Ship release", status: "waiting", session_id: source,
      ask: "Can we ship?", links: [{ ref: "https://example.com/pull/7" }] },
  };
  data.sources = Object.fromEntries(
    ["decisions", "goals", "watches", "tasks", "schedules", "background", "activity", "directives"]
      .map((kind) => [kind, { items: kind === "tasks" ? [item] : [], status: "ready",
        error: null, loadedAt: 1, retry: vi.fn() } as WorkSource]),
  ) as WorkItems;
  vi.spyOn(api, "chatSessionTurns").mockResolvedValue({ turns: [] });
  useUI.setState({ surfaceStates: {}, surfaceModal: null });
  beginThisChatVisit(); // A previous chat-scoped visit must not hide this fleet-wide item.
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onNavigate = vi.fn();
  try {
    await act(async () => root.render(
      <QueryClientProvider client={client}><ToastProvider>
        <NeedsYouSection items={[item]} held={new Map()} onHold={vi.fn()} records={{}}
          answer={vi.fn()} mobile={false} onNavigate={onNavigate}
          openWork={(itemId) => openWorkGroup("needs-you", itemId)} goalsRetry={vi.fn()} />
        <WorkView frame="rail" sessionId="unrelated-chat" />
      </ToastProvider></QueryClientProvider>,
    ));
    await act(async () => host.querySelector<HTMLButtonElement>(".chat-needs-you__task button")!.click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(onNavigate).toHaveBeenCalledOnce();
    const detail = host.querySelector(".work-detail")!;
    expect(detail).toBeTruthy();
    expect(detail.textContent).toContain("Can we ship?");
    expect(detail.querySelector("a")?.getAttribute("href")).toBe("https://example.com/pull/7");
    expect(detail.textContent).toContain("Started in an agent-to-agent conversation");
    const labels = [...detail.querySelectorAll("button")].map((button) => button.textContent);
    expect(labels).toEqual(expect.arrayContaining(["Send reply", "Done…", "Dismiss…"]));
    expect(labels).not.toContain("Open chat");
    expect(openChat).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain("Couldn't");
    expect(document.querySelector(".pl-toast")).toBeNull();
  } finally {
    await act(async () => root.unmount());
    client.clear();
    host.remove();
  }
});
