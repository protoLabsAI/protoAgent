// The next-call preview tab's FETCH LIFECYCLE (#3257) — not the formatters.
//
// The first cut of this tab shipped an effect that self-cancelled: `previewState`
// was both set inside the effect and listed in its dep array, so
// setPreviewState("loading") re-ran the effect, the cleanup flipped `alive =
// false` (discarding the in-flight response), and the re-run's own guard
// returned early because the state was no longer "idle". The tab sat on
// "Composing the next call…" forever. Every unit test at the time targeted the
// pure helpers in promptView.ts, so nothing exercised the component and the bug
// shipped green. These tests drive the real component through a mocked api.
//
// jsdom + react-dom/client, no @testing-library (the console has none) and the
// unit harness is .test.ts only, so elements are built with createElement.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../lib/api";
import type { PromptCall } from "../../lib/types";
import { PromptViewerBody } from "../PromptViewer";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CALL: PromptCall = {
  call_index: 0,
  ts: "2026-08-28T10:00:00+00:00",
  model: "protolabs/smart",
  system: { stable: "STABLE PROMPT", context: "" },
  projected_context: "\n\nPROJECTED CONTEXT",
  sections: [{ label: "SOUL", chars: 13, approx_tokens: 3, scope: "stable" }],
  usage: { input_tokens: 100, output_tokens: 10, cache_read_tokens: 0, cache_creation_tokens: 0 },
};

const PREVIEW_CALL: PromptCall = {
  ...CALL,
  call_index: -1,
  preview: true,
  speculative: true,
  projected_context: "\n\nNEXT CALL PROJECTION",
  budget: { chars: 16000, used: 15008, overflow: [{ label: "RAG hits", dropped_items: 11, dropped_chars: 11550 }] },
  sections: [{ label: "Injected memory", chars: 12165, approx_tokens: 3041, scope: "projected", truncated: true }],
};

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.spyOn(api, "promptsForTask").mockResolvedValue({ enabled: true, calls: [CALL], subagents: [], prev: null });
  vi.spyOn(api, "promptBreakdown").mockResolvedValue({ found: false });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

async function mount() {
  await act(async () => {
    root.render(h(PromptViewerBody, { taskId: "task-1", sessionId: "sess-1" }));
  });
  await settle();
}

/** Let queued microtasks + timers flush; React commits async resolutions on a
 *  follow-up tick, so a single await is not enough. */
async function settle(rounds = 10) {
  for (let i = 0; i < rounds; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

function previewTab(): HTMLElement {
  const el = [...container.querySelectorAll("button, [role='tab']")].find((b) =>
    /Next call \(preview\)/.test(b.textContent ?? ""),
  );
  if (!el) throw new Error("no preview tab rendered");
  return el as HTMLElement;
}

/** A promise this test resolves by hand.
 *
 *  Timing matters here and an immediately-resolved mock does NOT reproduce the
 *  bug: it settles on the first microtask, often before React has re-rendered
 *  and re-run the effect, so the self-cancel never happens and the test passes
 *  against broken code. Holding the response open until after React has had its
 *  chance to re-run is what makes this deterministic — verified by re-arming the
 *  dep array and watching these fail. */
function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void } {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

type PreviewResponse = { enabled: boolean; call: PromptCall | null; reason?: string };

describe("PromptViewer — next-call preview tab", () => {
  it("reaches the loaded preview instead of sticking on the loading state", async () => {
    const d = deferred<PreviewResponse>();
    const spy = vi.spyOn(api, "promptPreview").mockReturnValue(d.promise);
    await mount();

    await act(async () => {
      previewTab().click();
    });
    // Give React every chance to re-render and re-run the effect while the
    // request is still in flight — the window the self-cancel lived in.
    await settle();
    expect(container.textContent).toContain("Composing the next call");

    await act(async () => {
      d.resolve({ enabled: true, call: PREVIEW_CALL });
    });
    await settle();

    // The regression: the effect self-cancelled and this text never cleared.
    expect(container.textContent).not.toContain("Composing the next call");
    // The preview's own body is what renders — not this turn's captured call.
    expect(container.textContent).toContain("NEXT CALL PROJECTION");
    expect(container.textContent).not.toContain("PROJECTED CONTEXT");
    // Speculative banner + the delivery budget the preview carried.
    expect(container.textContent).toContain("Speculative");
    expect(container.textContent).toContain("Delivery budget");
    expect(container.textContent).toContain("Shed to fit");
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith("sess-1");
  });

  it("does not fetch until the tab is selected, and does not re-fetch after", async () => {
    const d = deferred<PreviewResponse>();
    const spy = vi.spyOn(api, "promptPreview").mockReturnValue(d.promise);
    await mount();

    // The speculation re-runs retrieval, so it must not ride the dialog opening.
    expect(spy).not.toHaveBeenCalled();

    await act(async () => {
      previewTab().click();
    });
    await settle();
    await act(async () => {
      d.resolve({ enabled: true, call: PREVIEW_CALL });
    });
    await settle();
    expect(spy).toHaveBeenCalledTimes(1);

    // Switching away and back re-runs the effect (active changes) but the guard
    // holds — a second speculative retrieval would be a real cost.
    const callTab = [...container.querySelectorAll("button, [role='tab']")].find((b) =>
      /^Call 1$/.test(b.textContent?.trim() ?? ""),
    ) as HTMLElement;
    await act(async () => {
      callTab.click();
    });
    await settle(2);
    await act(async () => {
      previewTab().click();
    });
    await settle(2);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("NEXT CALL PROJECTION");
  });

  it("surfaces the server's reason when the preview is unavailable", async () => {
    const d = deferred<PreviewResponse>();
    vi.spyOn(api, "promptPreview").mockReturnValue(d.promise);
    await mount();

    await act(async () => {
      previewTab().click();
    });
    await settle();
    await act(async () => {
      d.resolve({ enabled: true, call: null, reason: "live graph has no prompt stamps" });
    });
    await settle();

    expect(container.textContent).not.toContain("Composing the next call");
    expect(container.textContent).toContain("live graph has no prompt stamps");
  });

  it("shows the error instead of hanging when the preview request fails", async () => {
    const d = deferred<PreviewResponse>();
    vi.spyOn(api, "promptPreview").mockReturnValue(d.promise);
    await mount();

    await act(async () => {
      previewTab().click();
    });
    await settle();
    await act(async () => {
      d.reject(new Error("boom"));
      await d.promise.catch(() => undefined);
    });
    await settle();

    expect(container.textContent).not.toContain("Composing the next call");
    expect(container.textContent).toContain("boom");
  });
});
