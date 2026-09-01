// ADR 0057 — where a launcher row's OUTCOME goes.
//
// The desktop launcher is a frameless window that hides itself the moment the palette
// closes (`onOpenChange(false)` → `hide_launcher` → `window.hide()` in the Rust shell), and
// every compiled `tool`/`emit` row closes the palette as it fires. So a toast raised in this
// window after the request settles renders into a webview nobody can see — the exact
// "reports its outcome instead of failing silently" claim, failing silently. The outcome is
// forwarded to the console window instead, which is raised the same way a `navigate` row
// already raises it.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Launcher } from "./Launcher";
import {
  forwardPaletteNotice,
  paletteNoticeFrom,
  useForwardedPaletteNotices,
  PALETTE_NOTICE_EVENT,
} from "./usePaletteRegistry";

// jsdom gaps this file needs: React only honors `act()` when the environment opts in, and
// the DS palette scrolls its selected row into view on mount. Set per-file rather than in
// vitest.setup.ts — the act flag turns every un-acted update in a file into a warning, and
// the other suites render without it on purpose.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};

// ── The seam itself ──────────────────────────────────────────────────────────────

type TauriStub = {
  emit: ReturnType<typeof vi.fn>;
  invoke: ReturnType<typeof vi.fn>;
  listen: ReturnType<typeof vi.fn>;
};

function installShell(): TauriStub {
  const stub: TauriStub = {
    emit: vi.fn(async () => {}),
    invoke: vi.fn(async () => undefined),
    listen: vi.fn(async () => () => {}),
  };
  (window as unknown as { __TAURI__?: unknown }).__TAURI__ = {
    core: { invoke: stub.invoke },
    event: { emit: stub.emit, listen: stub.listen },
  };
  return stub;
}

function removeShell() {
  delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
}

afterEach(() => {
  removeShell();
  vi.restoreAllMocks();
});

describe("forwardPaletteNotice", () => {
  it("hands the outcome to the console window and raises it, never to this hidden one", () => {
    const shell = installShell();
    const local = vi.fn();
    forwardPaletteNotice(local)({ tone: "error", title: "Reindex failed", message: "404 Not Found" });
    expect(local).not.toHaveBeenCalled();
    expect(shell.emit).toHaveBeenCalledWith(PALETTE_NOTICE_EVENT, {
      tone: "error",
      title: "Reindex failed",
      message: "404 Not Found",
    });
    // Raised, or the message lands on a window that is behind whatever the operator
    // summoned the launcher from — visible in principle, unseen in practice.
    expect(shell.invoke).toHaveBeenCalledWith("focus_main", undefined);
  });

  it("falls back to the local toast when there is no other window", () => {
    // No desktop shell: a test host, or a fork mounting the palette on a plain page. There
    // is nothing to forward to, and a dropped outcome is the failure this exists to fix.
    const local = vi.fn();
    forwardPaletteNotice(local)({ tone: "success", message: "ok" });
    expect(local).toHaveBeenCalledWith({ tone: "success", message: "ok" });
  });
});

describe("paletteNoticeFrom", () => {
  it("keeps a well-formed notice and normalizes an unknown tone to an error", () => {
    expect(paletteNoticeFrom({ tone: "success", title: " Reindex ", message: " done " })).toEqual({
      tone: "success",
      title: "Reindex",
      message: "done",
    });
    // An outcome we cannot classify is not a success.
    expect(paletteNoticeFrom({ tone: "whatever", message: "x" })).toEqual({ tone: "error", message: "x" });
  });

  it("drops a payload that would render a blank toast", () => {
    // It arrives as an untyped cross-window event payload; a toast built from `undefined` is
    // a card with nothing on it that the operator cannot act on.
    for (const bad of [null, undefined, "nope", 42, {}, { tone: "error" }, { message: "   " }]) {
      expect(paletteNoticeFrom(bad), JSON.stringify(bad) ?? "undefined").toBeNull();
    }
  });
});

describe("useForwardedPaletteNotices", () => {
  it("toasts a forwarded notice in the console window, and ignores an unusable payload", async () => {
    // The other end of the handoff. Without it the launcher's `emit` goes nowhere and the
    // outcome is lost in a different way than before — which is why the seam is tested from
    // both sides rather than only where the bug was.
    const shell = installShell();
    let deliver: ((e: { payload: unknown }) => void) | undefined;
    shell.listen.mockImplementation(async (_event: string, handler: (e: { payload: unknown }) => void) => {
      deliver = handler;
      return () => {};
    });
    const notify = vi.fn();
    const host = document.createElement("div");
    document.body.appendChild(host);
    const Probe = () => {
      useForwardedPaletteNotices(notify);
      return null;
    };
    await act(async () => {
      root = createRoot(host);
      root.render(h(Probe));
    });
    await vi.waitFor(() => expect(deliver).toBeTruthy());
    act(() => deliver!({ payload: { tone: "error", title: "Reindex failed", message: "404 Not Found" } }));
    expect(notify).toHaveBeenCalledWith({ tone: "error", title: "Reindex failed", message: "404 Not Found" });
    act(() => deliver!({ payload: { tone: "error" } }));
    expect(notify).toHaveBeenCalledTimes(1); // a blank toast is not an outcome
  });
});

// ── The wiring ───────────────────────────────────────────────────────────────────
// The seam is only half of it: the Launcher has to actually PASS it as the palette's
// `notify`. That one line is the whole bug, so it gets a mount rather than a source read.

let root: Root | null = null;

beforeEach(() => {
  document.body.innerHTML = "";
});

afterEach(() => {
  if (root) {
    const r = root;
    act(() => r.unmount());
    root = null;
  }
  document.body.innerHTML = "";
});

const STATUS = {
  identity: { name: "protoAgent" },
  plugins: [
    {
      id: "files",
      name: "Files",
      enabled: true,
      loaded: true,
      tools: [],
      skills: 0,
      views: [],
      commands: [{ id: "reindex", title: "Reindex workspace", action: { type: "tool", route: "reindex", method: "POST" } }],
    },
  ],
};

function stubFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(typeof input === "string" || input instanceof URL ? input : input.url);
    if (url.includes("/api/runtime/status")) {
      return new Response(JSON.stringify(STATUS), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (url.includes("/api/plugins/files/reindex")) return new Response("no such route", { status: 404 });
    return new Promise<Response>(() => {}); // the fleet poll and friends: hang, never resolve
  });
}

describe("Launcher — a tool row's outcome", () => {
  it("forwards the failure to the console window instead of toasting into a hidden one", async () => {
    const shell = installShell();
    stubFetch();
    const host = document.createElement("div");
    document.body.appendChild(host);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    await act(async () => {
      root = createRoot(host);
      root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(Launcher))));
    });

    // The row is registered off the status payload; wait for it to reach the DS list.
    let row: HTMLElement | undefined;
    await vi.waitFor(() => {
      row = [...document.querySelectorAll(".pl-cmdk-commands__label")].find(
        (el) => el.textContent === "Reindex workspace",
      )?.closest<HTMLElement>("[role='option'], button, div[class*='pl-cmdk-commands__item']") ?? undefined;
      expect(row, "no Reindex row in the launcher palette").toBeTruthy();
    });

    await act(async () => {
      row!.click();
    });
    // The POST 404s; the error has to leave this window.
    await vi.waitFor(() =>
      expect(shell.emit).toHaveBeenCalledWith(PALETTE_NOTICE_EVENT, expect.objectContaining({ tone: "error" })),
    );
    // …and NOT into the launcher's own toast stack, which `hide_launcher` just took offscreen.
    expect(document.querySelector(".pl-toast")).toBeNull();
  });
});
