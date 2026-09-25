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

// bootStuck fires at 45s; the probe's retry budget (60 retries × retryDelay 1000ms) errors at ~60s;
// bootFailed is now gated on its own 120s elapsed-time timer (bd-wrdh), NOT on the retry count.
const BOOT_STUCK_S = 45;
const BOOT_FAILED_S = 120;

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

describe("App boot 'isn't responding' gate is time-based, not retry-count-based", () => {
  it("stays stuck (not failed) after the probe errors, until the 120s elapsed-time timeout", async () => {
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

    // Walk second-by-second (past the 120s failed timer, with margin) so a transient failure gate
    // can't slip between coarse samples. The probe's retry budget errors in a short pulse each ~60s
    // cycle (isError flips true the instant retries exhaust, then refetchInterval starts the next
    // cycle) — the first pulse falls at ~60s, the next at ~120s+, deterministic under fake timers.
    const timeline: Array<ReturnType<typeof bootPhase>> = [];
    for (let s = 1; s <= BOOT_FAILED_S + 15; s++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      timeline.push(bootPhase());
    }

    // At the 45s threshold the gate switches to the reassuring "taking longer than usual /
    // Continue anyway" copy — the gentle path shown before any failure gate.
    expect(timeline[BOOT_STUCK_S - 1]).toBe("stuck"); // second 45

    // The core guarantee of bd-wrdh: for the WHOLE first 120s the operator must NEVER see the
    // alarming "isn't responding" gate — not before the 45s stuck path, and crucially not even once
    // the ~60s retry budget is spent and the probe query ERRORS. bootFailed is gated on 120s of
    // elapsed time, not on the retry count, so the ~60s error pulse still reads as "stuck".
    const beforeFailedTimeout = timeline.slice(0, BOOT_FAILED_S - 1); // seconds 1..119
    expect(beforeFailedTimeout).not.toContain("failed");

    // Only once the 120s elapsed-time budget is genuinely spent does the failure gate surface (the
    // first errored probe at/after 120s) — a real timeout for an engine that never came up, not a
    // false alarm mid-startup.
    const afterTimeout = timeline.slice(BOOT_FAILED_S - 1); // seconds 120..135
    expect(afterTimeout).toContain("failed");
  });
});
