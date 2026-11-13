// Feed slice 5 (docs/design/feed.md §5, §8): clicking a row swaps the dialog body to the
// row's detail — "← Feed" + focused heading, the dl fields, the response as markdown (or
// the failure reason / the queued status sentence), and the open-in-reader action, which
// now lives ONLY here, not on the list rows. Rendered through ActivitySurface with a
// mocked api, same harness as ActivityWidget.test.tsx (createRoot/act, no testing-library).
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ActivityEntry, InboxItem, RuntimeStatus } from "../lib/types";

const mocks = vi.hoisted(() => ({
  openDocument: vi.fn(),
  listeners: new Map<string, Array<(data: Record<string, unknown>) => void>>(),
}));

vi.mock("../lib/events", () => ({
  onServerEvent: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    mocks.listeners.set(topic, [...(mocks.listeners.get(topic) ?? []), fn]);
    return () => {};
  },
}));
// The real Markdown lazily pulls the heavy pipeline — render the raw source instead.
vi.mock("../chat/LazyMarkdown", () => ({
  Markdown: ({ children }: { children: string }) => <div className="md">{children}</div>,
}));
vi.mock("../docviewer", () => ({ openDocument: mocks.openDocument }));

import { api } from "../lib/api";
import { ActivitySurface } from "./ActivitySurface";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function inboxItem(over: Partial<InboxItem> = {}): InboxItem {
  return {
    id: 1, created_at: "2026-09-03T10:00:00Z", priority: "next", source: "a2a:sister",
    text: "an ordinary queued stimulus", dedup_key: null, delivered_at: null, ...over,
  };
}

function entry(over: Partial<ActivityEntry> = {}): ActivityEntry {
  return {
    id: 10, created_at: "2026-09-03T11:00:00Z", origin: "scheduler", trigger: "heartbeat",
    priority: "", state: "completed", text: "Docs deployed.\n\nAll **green**.", task_id: "", stimulus: "", ...over,
  };
}

let container: HTMLElement;
let root: Root;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function renderFeed(opts: { items?: InboxItem[]; entries?: ActivityEntry[] } = {}) {
  vi.spyOn(api, "inbox").mockResolvedValue({ items: opts.items ?? [] });
  vi.spyOn(api, "activity").mockResolvedValue({ context_id: "s1", entries: opts.entries ?? [], messages: [] });
  vi.spyOn(api, "runtimeStatus").mockResolvedValue({
    identity: { name: "roxy", operator: "" },
  } as unknown as RuntimeStatus);
  act(() =>
    root.render(
      h(
        QueryClientProvider,
        { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
        h(ActivitySurface),
      ),
    ),
  );
  await flush();
}

const q = <T extends Element = Element>(sel: string) => container.querySelector<T>(sel);
const openRow = (at = 0) =>
  act(() => container.querySelectorAll<HTMLButtonElement>(".feed-row")[at].click());
const emit = (topic: string, data: Record<string, unknown>) =>
  act(() => (mocks.listeners.get(topic) ?? []).forEach((fn) => fn(data)));

beforeEach(() => {
  mocks.openDocument.mockClear();
  mocks.listeners.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("FeedDetail (via ActivitySurface) — docs/design/feed.md §5, §8", () => {
  it("a ran row: detail replaces the list, focuses the heading, shows the response as markdown, opens the reader", async () => {
    const WAKE = "[Autonomous wake — scheduled run. Orient from <working_state>, then:]";
    await renderFeed({ entries: [entry({ stimulus: `${WAKE}\n\nCheck the board.` })] });

    // The open-in-reader action no longer exists on list rows — detail only.
    expect(q('[aria-label="Open in reader"]')).toBeNull();

    openRow();
    expect(q('[data-testid="feed-detail"]')).not.toBeNull();
    expect(q('[data-testid="feed-list"]')).toBeNull(); // same dialog body, list swapped out

    const heading = q(".feed-detail-title")!;
    expect(heading.tagName).toBe("H3");
    expect(heading.textContent).toBe("Docs deployed.");
    expect(document.activeElement).toBe(heading);

    const dl = q(".feed-detail-fields")!;
    expect(dl.textContent).toContain("What triggered it");
    expect(dl.textContent).toContain("Scheduled · heartbeat");
    expect(dl.textContent).toContain("In response to");
    expect(dl.textContent).toContain("Check the board."); // wake header stripped
    expect(dl.textContent).not.toContain("Autonomous wake");
    // Response rides the markdown component (mocked to echo its raw source).
    expect(q(".feed-detail-response .md")!.textContent).toBe("Docs deployed.\n\nAll **green**.");

    act(() => q<HTMLButtonElement>('[aria-label="Open in reader"]')!.click());
    expect(mocks.openDocument).toHaveBeenCalledTimes(1);
    const doc = mocks.openDocument.mock.calls[0][0] as { title: string; content: string };
    expect(doc.title).toBe("Scheduled · heartbeat");
    expect(doc.content).toContain("> **In response to**");
    expect(doc.content).toContain("Check the board.");
    expect(doc.content).toContain("All **green**.");
  });

  it("a failed row: the detail shows What went wrong with the entry's text, not a Response block", async () => {
    await renderFeed({ entries: [entry({ state: "failed", text: "Second target timed out." })] });

    openRow();
    const dl = q(".feed-detail-fields")!;
    expect(dl.textContent).toContain("What went wrong");
    expect(q(".feed-detail-reason")!.textContent).toBe("Second target timed out.");
    expect(dl.textContent).not.toContain("Response");
  });

  it("a queued item: the detail promises the run in plain words and offers no reader", async () => {
    await renderFeed({ items: [inboxItem({ text: "Deploy the docs" })] });

    openRow();
    expect(q(".feed-detail-fields")!.textContent).toContain(
      "Queued. It runs when Roxy finishes what it's doing now. Nothing for you to do.",
    );
    expect(q('[aria-label="Open in reader"]')).toBeNull();
  });

  it("a queued item whose retries are used up points at Work › Needs you, never 'nothing to do'", async () => {
    await renderFeed({ items: [inboxItem({ retries_exhausted: true })] });

    openRow();
    const status = q(".feed-detail-fields")!.textContent!;
    expect(status).toContain("automatic retries are used up");
    expect(status).toContain("Work › Needs you");
    expect(status).not.toContain("Nothing for you to do");
  });

  it("a live result replaces the open queued detail in place (§3) — no stale Queued page", async () => {
    await renderFeed({ items: [inboxItem({ id: 7, text: "Deploy the docs" })] });

    openRow();
    expect(q(".feed-detail-fields")!.textContent).toContain("Queued.");

    emit("activity.message", { text: "Docs are live.", origin: "inbox", inbox_id: 7 });
    expect(q(".feed-detail-fields")!.textContent).not.toContain("Queued");
    expect(q(".feed-detail-response .md")!.textContent).toBe("Docs are live.");
    expect(q('[aria-label="Open in reader"]')).not.toBeNull();

    // ← Feed now targets the entry row standing where the queued row was.
    const back = [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === "← Feed")!;
    act(() => back.click());
    expect((document.activeElement as HTMLElement).getAttribute("data-key")).toBe("entry:-1");
  });

  it("← Feed returns to the list and puts focus back on the row that opened the detail", async () => {
    await renderFeed({
      entries: [entry({ id: 1, created_at: "2026-09-03T09:00:00Z" }), entry({ id: 2, text: "newest" })],
    });

    openRow(1); // the older row — entry:1, second in the newest-first list
    expect(document.activeElement).toBe(q(".feed-detail-title"));

    const back = [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === "← Feed")!;
    act(() => back.click());

    expect(q('[data-testid="feed-list"]')).not.toBeNull();
    expect(q('[data-testid="feed-detail"]')).toBeNull();
    const focused = document.activeElement as HTMLElement;
    expect(focused.classList.contains("feed-row")).toBe(true);
    expect(focused.getAttribute("data-key")).toBe("entry:1");
  });
});
