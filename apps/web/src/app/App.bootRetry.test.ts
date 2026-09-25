import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Surface the boot gate's title + detail so the test can read WHICH phase App resolves —
// loading / stuck / failed all render the same BootGate with different copy.
vi.mock("@protolabsai/ui/splash", () => ({
  Splash: () => null,
  BootGate: ({ title, detail }: { title?: ReactNode; detail?: ReactNode }) =>
    h(
      "div",
      { "data-testid": "boot-gate" },
      h("div", { "data-testid": "boot-title" }, title),
      h("div", { "data-testid": "boot-detail" }, detail),
    ),
}));

vi.mock("./AuthGate", () => ({ AuthGate: () => null }));
vi.mock("./UpdateNotice", () => ({ UpdateNotice: () => null }));

import { App } from "./App";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// bootStuck fires at 45s; the probe's retry budget (60 retries × retryDelay 1000ms) is ~60s.
const BOOT_STUCK_S = 45;

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** Classify the boot gate's current copy into its phase (mirrors App's title/detail table). */
function bootPhase(): "loading" | "stuck" | "failed" | "none" {
  if (!document.querySelector('[data-testid="boot-gate"]')) return "none";
  const title = document.querySelector('[data-testid="boot-title"]')?.textContent ?? "";
  const detail = document.querySelector('[data-testid="boot-detail"]')?.textContent ?? "";
  if (title.includes("responding")) return "failed"; // "<engine> isn't responding"
  if (detail.includes("taking longer than usual")) return "stuck";
  return "loading";
}

describe("App boot probe retry budget outlasts the bootStuck timer", () => {
  it("never shows the harsh failure gate before the 45s stuck path, then times out only past the ~60s budget", async () => {
    vi.useFakeTimers();
    // Every runtime probe fails as if the sidecar hasn't bound its port yet. A plain rejection
    // (not an ApiError 401/409/502) keeps this on the generic engine boot path, not a
    // focused-agent recovery (currentSlug() is "host" in jsdom).
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("connection refused"));
    // App owns the probe's own retry predicate; pin the client default off so no unrelated
    // query's retries interfere with the timeline.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    await act(async () => {
      root.render(h(QueryClientProvider, { client: queryClient }, h(App)));
    });

    // Walk second-by-second so a transient failure gate can't slip between coarse samples.
    const timeline: Array<ReturnType<typeof bootPhase>> = [];
    for (let s = 1; s <= 66; s++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      timeline.push(bootPhase());
    }

    // The core guarantee: while a slow cold start is still probing, the operator must NEVER see
    // the alarming "isn't responding" gate before the gentle bootStuck message. With the old
    // 30-retry budget the probe gave up (~30s) and flashed "failed" at ~31s — well before the
    // 45s stuck timer. The 60-retry budget outlasts bootStuck, so this window stays "loading".
    const beforeStuck = timeline.slice(0, BOOT_STUCK_S - 1); // seconds 1..44
    expect(beforeStuck).not.toContain("failed");

    // At the 45s threshold the gate switches to the reassuring "taking longer than usual /
    // Continue anyway" copy — the gentle path the retry budget is meant to reveal first.
    expect(timeline[BOOT_STUCK_S - 1]).toBe("stuck"); // second 45

    // Once the ~60s budget is genuinely spent the failure gate DOES surface — bootFailed is now a
    // real timeout for an engine that never came up, not a false alarm during normal startup.
    const afterBudget = timeline.slice(BOOT_STUCK_S); // seconds 46..66
    expect(afterBudget).toContain("failed");
  });
});
