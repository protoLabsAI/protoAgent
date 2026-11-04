// The trace-id copy button reports the clipboard outcome, and the toast only claims
// success when the write resolved (#4733 QA: it used to toast "copied" regardless).
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TraceCell } from "./TelemetrySurface";
import { traceCopyToast } from "./traceUrl";
import type { TelemetryTurn } from "../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TRACE = "0123456789abcdef";
let container: HTMLElement;
let root: Root;

function stubClipboard(value: unknown) {
  Object.defineProperty(navigator, "clipboard", { value, configurable: true });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  stubClipboard(undefined);
});

async function clickCopy(onCopied: (ok: boolean, id: string) => void) {
  const turn = { trace_id: TRACE } as TelemetryTurn;
  act(() => root.render(h(TraceCell, { turn, template: null, tracingEnabled: true, onCopied })));
  const btn = container.querySelector<HTMLButtonElement>('[data-testid="telemetry-trace-copy"]');
  if (!btn) throw new Error("no copy button");
  await act(async () => {
    btn.click();
  });
}

describe("TraceCell copy", () => {
  it("reports success once the write resolves", async () => {
    stubClipboard({ writeText: vi.fn().mockResolvedValue(undefined) });
    const onCopied = vi.fn();
    await clickCopy(onCopied);
    expect(onCopied).toHaveBeenCalledWith(true, TRACE);
  });

  it("reports failure when the write rejects", async () => {
    stubClipboard({ writeText: vi.fn().mockRejectedValue(new Error("denied")) });
    const onCopied = vi.fn();
    await clickCopy(onCopied);
    expect(onCopied).toHaveBeenCalledWith(false, TRACE);
  });

  it("reports failure when the Clipboard API is missing", async () => {
    stubClipboard(undefined);
    const onCopied = vi.fn();
    await clickCopy(onCopied);
    expect(onCopied).toHaveBeenCalledWith(false, TRACE);
  });
});

describe("traceCopyToast", () => {
  it("only says copied on success", () => {
    expect(traceCopyToast(true, TRACE).title).toBe("Trace id copied");
  });

  it("is an error carrying the id on failure, so it can still be selected", () => {
    const t = traceCopyToast(false, TRACE);
    expect(t.tone).toBe("error");
    expect(t.title).not.toMatch(/copied/i);
    expect(t.message).toBe(TRACE);
  });
});
