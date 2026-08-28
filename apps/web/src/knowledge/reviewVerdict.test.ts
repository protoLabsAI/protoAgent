import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
import type { KnowledgeChunk } from "../lib/types";
import { ReviewActions, ReviewChip, resetReviewSupport, reviewStateOf } from "./ReviewVerdict";

// Review verdicts (ADR 0108 D7) — the console half of the memory write lifecycle.
// Renders the real components against a mocked `api` (the same createRoot + act
// harness the settings UI tests use), so the assertions cover the request body the
// backend relies on (tier), the optimistic cache flip, and the failure paths.

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;
let client: QueryClient;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function chunk(over: Partial<KnowledgeChunk> = {}): KnowledgeChunk {
  return {
    id: 7,
    heading: "coffee",
    content: "The operator takes coffee black",
    preview: "The operator takes coffee black",
    domain: "preferences",
    source: "conversation",
    source_type: "conversation",
    finding_type: null,
    created_at: "2026-08-28T10:00:00+00:00",
    tier: "private",
    review_state: "pending",
    ...over,
  };
}

function render(c: KnowledgeChunk) {
  act(() => {
    root.render(
      h(
        QueryClientProvider,
        { client },
        h(ToastProvider, null, h("div", null, h(ReviewChip, { chunk: c }), h(ReviewActions, { chunk: c }))),
      ),
    );
  });
}

const button = (label: string) => container.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`);
// The verdict actions only — the ToastProvider mounts its own dismiss buttons in here too.
const actionCount = () => container.querySelectorAll('button[aria-label$=" entry 7"]').length;
const failWith = (status: number, detail: string) => Object.assign(new Error(detail), { status });

beforeEach(() => {
  resetReviewSupport();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("reviewStateOf", () => {
  it("normalizes the backend value and hides anything it can't act on", () => {
    expect(reviewStateOf({ review_state: "confirmed" })).toBe("confirmed");
    expect(reviewStateOf({ review_state: " Pending " })).toBe("pending");
    expect(reviewStateOf({ review_state: "candidate" })).toBeNull(); // pre-D7 free text
    expect(reviewStateOf({ review_state: null })).toBeNull();
    expect(reviewStateOf({})).toBeNull();
  });
});

describe("ReviewActions", () => {
  it("confirms a private pending row, sending the tier the route needs, and flips the cached chip", async () => {
    const review = vi.spyOn(api, "reviewMemoryChunk").mockResolvedValue({ enabled: true, id: 7, review_state: "confirmed" });
    const row = chunk();
    // Seed the knowledge list cache the way the Store view keys it, so the optimistic
    // update has a row to flip before the server answers.
    const key = [...queryKeys.knowledge, ""] as const;
    client.setQueryData(key, { enabled: true, query: "", results: [row], stats: {} });
    vi.spyOn(api, "knowledgeSearch").mockResolvedValue({ enabled: true, query: "", results: [{ ...row, review_state: "confirmed" }], stats: {} });

    render(row);
    expect(container.querySelector("[data-review-state]")?.getAttribute("data-review-state")).toBe("pending");
    expect(button("confirm entry 7")).not.toBeNull();
    expect(button("reject entry 7")).not.toBeNull();
    expect(button("reopen entry 7")).toBeNull();

    act(() => button("confirm entry 7")!.click());
    await flush();
    await flush();

    expect(review).toHaveBeenCalledWith(7, { state: "confirmed", tier: "private" });
    const cached = client.getQueryData<{ results: KnowledgeChunk[] }>(key);
    expect(cached?.results[0].review_state).toBe("confirmed");
    expect(document.body.textContent).toContain("Marked confirmed");
  });

  it("omits tier for an untiered store and offers re-open on a rejected row", async () => {
    const review = vi.spyOn(api, "reviewMemoryChunk").mockResolvedValue({ enabled: true, id: 7, review_state: "pending" });
    render(chunk({ tier: null, review_state: "rejected" }));
    expect(button("confirm entry 7")).toBeNull();
    expect(button("reject entry 7")).toBeNull();
    act(() => button("reopen entry 7")!.click());
    await flush();
    expect(review).toHaveBeenCalledWith(7, { state: "pending" });
  });

  it("shows the chip but no actions for a commons row", () => {
    render(chunk({ tier: "commons", review_state: "pending" }));
    expect(container.querySelector("[data-review-state]")?.getAttribute("data-review-state")).toBe("pending");
    expect(actionCount()).toBe(0);
  });

  it("treats a row with no verdict as pending and shows nothing on the chip", () => {
    render(chunk({ review_state: null }));
    expect(container.querySelector("[data-review-state]")).toBeNull();
    expect(button("confirm entry 7")).not.toBeNull();
    expect(button("reject entry 7")).not.toBeNull();
  });

  it("hides the actions once the backend answers 501 (no verdicts on this store)", async () => {
    vi.spyOn(api, "reviewMemoryChunk").mockRejectedValue(failWith(501, "this knowledge backend has no review verdicts"));
    render(chunk());
    act(() => button("confirm entry 7")!.click());
    await flush();
    await flush();
    expect(actionCount()).toBe(0);
    expect(document.body.textContent).toContain("doesn't support review verdicts");
  });

  it("surfaces the server detail verbatim on a 400 and rolls the chip back", async () => {
    vi.spyOn(api, "reviewMemoryChunk").mockRejectedValue(failWith(400, "commons rows are curated via promote/forget"));
    const row = chunk();
    const key = [...queryKeys.knowledge, ""] as const;
    client.setQueryData(key, { enabled: true, query: "", results: [row], stats: {} });
    render(row);
    act(() => button("reject entry 7")!.click());
    await flush();
    await flush();
    expect(document.body.textContent).toContain("commons rows are curated via promote/forget");
    expect(client.getQueryData<{ results: KnowledgeChunk[] }>(key)?.results[0].review_state).toBe("pending");
    // Still actionable — a 400 is about this request, not the backend's capability.
    expect(button("confirm entry 7")).not.toBeNull();
  });

  it("treats enabled:false as a dropped write", async () => {
    vi.spyOn(api, "reviewMemoryChunk").mockResolvedValue({ enabled: false });
    render(chunk());
    act(() => button("confirm entry 7")!.click());
    await flush();
    await flush();
    expect(document.body.textContent).toContain("knowledge store is off");
  });
});

describe("knowledgeSearch review filter", () => {
  it("adds review_state to the listing query only when asked", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string | URL | Request) => {
        calls.push(String(url));
        return new Response(JSON.stringify({ enabled: true, query: "", results: [], stats: {} }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    await api.knowledgeSearch("");
    await api.knowledgeSearch("", { reviewState: "pending" });
    vi.unstubAllGlobals();
    expect(calls[0]).toContain("/api/knowledge/search?q=");
    expect(calls[0]).not.toContain("review_state");
    expect(calls[1]).toContain("review_state=pending");
  });
});
