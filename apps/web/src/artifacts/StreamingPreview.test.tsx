// StreamingPreview (ADR 0118 D3 console, S8b): the streamed artifact preview card + its
// sandboxed preview frame. createRoot/act + a hand-driven jsdom, like the other console UI suites
// (FrameComponentHost.test.tsx) — the console has no testing-library dep.
//
// The guarantees this slice MUST hold, one `it` each:
//   • the preview srcdoc carries the EXACT CSP, no allow-same-origin, and exactly one nonce'd script;
//   • a <script> or onclick= in the streamed markup never reaches the frame (stripped before posting);
//   • the preview is hidden before the gate opens and shown after; react/mermaid/vega-lite never
//     get a frame; and Idiomorph is vendored with its version + sha256 recorded.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PREVIEW_BODY_BYTE_THRESHOLD } from "./processPartialHtml";
import idiomorphLicense from "./vendor/idiomorph.LICENSE.txt?raw";
import idiomorphMin from "./vendor/idiomorph.min.js?raw";
import { StreamingPreview, type StreamingPreviewProps } from "./StreamingPreview";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
  vi.restoreAllMocks();
});

function render(props: StreamingPreviewProps) {
  act(() => root.render(h(StreamingPreview, props)));
}

function theFrame(): HTMLIFrameElement | null {
  return container.querySelector("iframe");
}

function byTestId(id: string): HTMLElement | null {
  return container.querySelector(`[data-testid="${id}"]`);
}

/** A buffer whose markup has a CLOSED <style>, so the preview gate is open. */
const GATE_OPEN = "<style>.x{color:red}</style><p>hi</p>";

describe("StreamingPreview — preview frame fence (r1)", () => {
  it("builds a srcdoc with the exact CSP, no allow-same-origin, and exactly one nonce'd script", () => {
    render({ buffer: { text: GATE_OPEN, done: false }, kind: "html", title: "Demo" });
    const frame = theFrame();
    expect(frame).not.toBeNull();

    // Sandbox: allow-scripts only, never allow-same-origin — the frame runs on an opaque origin.
    expect(frame!.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame!.getAttribute("sandbox")).not.toContain("allow-same-origin");
    // The markup is injected over postMessage, never the URL — no src, only srcdoc.
    expect(frame!.getAttribute("src")).toBeNull();

    const srcdoc = frame!.getAttribute("srcdoc") ?? "";

    // Exactly one script tag, and it is the one carrying the nonce (the vendored lib has none).
    const scriptTags = srcdoc.match(/<script\b[^>]*>/gi) ?? [];
    expect(scriptTags).toHaveLength(1);
    const nonce = scriptTags[0]?.match(/nonce="([0-9a-f]+)"/)?.[1];
    expect(nonce).toBeTruthy();

    // The EXACT CSP above, with this frame's nonce.
    expect(srcdoc).toContain(
      `content="default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline'; img-src data: blob:"`,
    );
    // The one nonce'd script carries the vendored morph lib + its receiver.
    expect(srcdoc).toContain("Idiomorph");
    expect(srcdoc).toContain("proto-preview:morph");
  });

  it("mints a fresh random nonce per frame", () => {
    render({ buffer: { text: GATE_OPEN, done: false }, kind: "html" });
    const first = theFrame()!.getAttribute("srcdoc")!.match(/nonce="([0-9a-f]+)"/)?.[1];
    act(() => root.unmount());
    root = createRoot(container);
    render({ buffer: { text: GATE_OPEN, done: false }, kind: "html" });
    const second = theFrame()!.getAttribute("srcdoc")!.match(/nonce="([0-9a-f]+)"/)?.[1];
    expect(first).toBeTruthy();
    expect(second).toBeTruthy();
    expect(first).not.toBe(second);
  });
});

describe("StreamingPreview — model code never reaches the frame (r2)", () => {
  it("strips a <script> and an onclick= from the streamed markup before posting", () => {
    const malicious = '<style>.x{color:red}</style><div onclick="steal()">hi</div><script>evil()</script>';
    render({ buffer: { text: malicious, done: false }, kind: "html" });

    const frame = theFrame()!;
    const win = frame.contentWindow as Window;
    const spy = vi.spyOn(win, "postMessage");
    // The host posts once the frame reports it has loaded (its receiver is then wired).
    act(() => {
      frame.dispatchEvent(new Event("load"));
    });

    const morph = spy.mock.calls.find((c) => (c[0] as { type?: string })?.type === "proto-preview:morph");
    expect(morph).toBeTruthy();
    const [message, target] = morph as [{ type: string; html: string }, string];

    // Opaque-origin frame → posted to "*"; the payload is the sanitised markup only.
    expect(target).toBe("*");
    expect(message.html).not.toMatch(/<script/i);
    expect(message.html).not.toMatch(/onclick/i);
    expect(message.html).toBe("<style>.x{color:red}</style><div>hi</div>");
  });
});

describe("StreamingPreview — gating + non-preview kinds (r3)", () => {
  it("hides the preview until the gate opens, then shows it", () => {
    // Style-less body below the 1.5 KB threshold → no frame yet, placeholder instead.
    render({ buffer: { text: "<p>hi</p>", done: false }, kind: "html" });
    expect(theFrame()).toBeNull();
    expect(byTestId("streaming-preview-placeholder")).not.toBeNull();

    // A closed <style> opens the gate → the preview frame appears, placeholder gone.
    render({ buffer: { text: GATE_OPEN, done: false }, kind: "html" });
    expect(theFrame()).not.toBeNull();
    expect(byTestId("streaming-preview-placeholder")).toBeNull();
  });

  it("opens the gate on 1.5 KB of style-less body markup", () => {
    render({ buffer: { text: "a".repeat(1535), done: false }, kind: "html" });
    expect(theFrame()).toBeNull();
    render({ buffer: { text: "a".repeat(1536), done: false }, kind: "html" });
    expect(theFrame()).not.toBeNull();
  });

  it("latches the gate open: a later unclosed <style> never retracts the frame, and the final chunk still reaches it", () => {
    // 1.5 KB of style-less body opens the gate on byte count alone; the frame mounts and loads.
    const opened = "a".repeat(PREVIEW_BODY_BYTE_THRESHOLD);
    render({ buffer: { text: opened, done: false }, kind: "html" });
    const frame = theFrame();
    expect(frame).not.toBeNull();

    const win = frame!.contentWindow as Window;
    const spy = vi.spyOn(win, "postMessage");
    act(() => {
      frame!.dispatchEvent(new Event("load"));
    });

    // A later still-opening <style> makes previewGateOpen() read false again (bodyBytesWithoutStyle
    // → 0, firstStyleClosed → false). The gate must NOT retract — the SAME frame element stays
    // mounted, so no remount can swallow an in-flight post. (Old, un-latched code unmounted here.)
    render({ buffer: { text: `${opened}<style>.x{color`, done: false }, kind: "html" });
    expect(theFrame()).toBe(frame);

    // The final chunk closes the <style> and carries the last body; it must still post to the frame.
    render({
      buffer: { text: `${opened}<style>.x{color:red}</style><p>final</p>`, done: true },
      kind: "html",
    });
    expect(theFrame()).toBe(frame);

    const morphCalls = spy.mock.calls.filter(
      (c) => (c[0] as { type?: string })?.type === "proto-preview:morph",
    );
    const lastCall = morphCalls[morphCalls.length - 1];
    const lastHtml = (lastCall?.[0] as { html?: string } | undefined)?.html ?? "";
    expect(lastHtml).toContain("final");
    expect(lastHtml).not.toMatch(/<script/i);
  });

  it.each(["react", "mermaid", "vega-lite"])("renders a placeholder only for %s, never a frame", (kind) => {
    // Even with gate-opening HTML in the buffer, a non-preview kind never mounts a frame.
    render({ buffer: { text: GATE_OPEN, done: false }, kind });
    expect(theFrame()).toBeNull();
    expect(byTestId("streaming-preview-placeholder")).not.toBeNull();
    expect(byTestId("streaming-preview-status")?.textContent).toBeTruthy();
  });

  it("shows the title when known and a kind-aware status line", () => {
    render({ buffer: { text: "", done: false }, kind: "mermaid", title: "Flow chart" });
    expect(byTestId("streaming-preview")?.textContent).toContain("Flow chart");
    expect(byTestId("streaming-preview-status")?.textContent).toMatch(/diagram/i);
  });

  it("falls back to a kind-derived name before the title is known", () => {
    render({ buffer: { text: "", done: false }, kind: "html" });
    expect(container.textContent).toContain("html artifact");
  });
});

describe("vendored Idiomorph (r4)", () => {
  it("is the pinned 0BSD build, safe to inline, recorded with version + sha256", () => {
    // The pinned release is an IIFE exposing `Idiomorph`.
    expect(idiomorphMin).toContain("Idiomorph=function");
    expect(idiomorphMin.length).toBeGreaterThan(1000);
    // No `</script` sequence, so inlining it in a <script> cannot close the tag early.
    expect(idiomorphMin).not.toMatch(/<\/script/i);
    // The license file records the pinned version, the 0BSD license and the sha256.
    expect(idiomorphLicense).toContain("0.7.3");
    expect(idiomorphLicense).toContain("0BSD");
    expect(idiomorphLicense).toContain(
      "8a7733fadfa0d3dae533fd80483c640885221c93458c1711c940797508f46662",
    );
  });
});
