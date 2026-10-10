// The artifact-ref chip's render states (#3617): latest, older ("v2 of 5"), deleted/evicted
// (inert), trimmed (inert), the panel off (inert), and the metadata route failing (still
// clickable). createRoot/act + the real uiStore, like the other console UI suites.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { resetPluginViewInbox, takePluginViewMessages } from "../lib/pluginViewInbox";
import { useUI } from "../state/uiStore";
import {
  ArtifactChatSendContext,
  ArtifactRefChip,
  createInlineFrameHost,
  InlineFrameBridgeContext,
  InlineFrameHostContext,
  type ArtifactChatSend,
} from "./ArtifactRefChip";
import { createFrameBridge } from "./frameBridge";
import { ARTIFACT_VIEW_KEY } from "./artifactRef";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

async function flush() {
  for (let i = 0; i < 3; i++) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

function meta(entry: { version_count: number; oldest: number } | null) {
  return vi.spyOn(api, "artifactRefs").mockResolvedValue({
    artifacts: entry ? { "a-1": { title: "Chart", kind: "html", ...entry } } : {},
  });
}

async function mount(props: Record<string, unknown>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => root.render(h(QueryClientProvider, { client: qc }, h(ArtifactRefChip, { props }))));
  await flush();
}

const REF = { artifact_id: "a-1", version: 2, versions_total: 2, title: "Chart", kind: "html" };

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  resetPluginViewInbox();
  window.matchMedia = ((q: string) => ({ matches: false, media: q })) as never;
  useUI.setState({ railOrder: { left: ["chat"], right: [ARTIFACT_VIEW_KEY], bottom: [], hidden: [] } });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

const chip = () => container.querySelector<HTMLButtonElement>('[data-testid="artifact-ref-chip"]');

describe("ArtifactRefChip", () => {
  it("latest version: title · v2 · kind, click opens the panel on it", async () => {
    meta({ version_count: 2, oldest: 1 });
    await mount(REF);
    const btn = chip();
    expect(btn).not.toBeNull();
    expect(btn!.textContent).toContain("Chart");
    expect(btn!.textContent).toContain("v2");
    expect(btn!.textContent).not.toContain("of");
    expect(btn!.textContent).toContain("html");
    expect(btn!.dataset.older).toBeUndefined();
    act(() => btn!.click());
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([{ type: "protoArtifact:select", id: "a-1", ver: 2 }]);
    expect(useUI.getState().rightPanel).toBe(ARTIFACT_VIEW_KEY);
  });

  it("older version reads 'v2 of 5' and still opens v2", async () => {
    meta({ version_count: 5, oldest: 1 });
    await mount(REF);
    expect(chip()!.textContent).toContain("v2 of 5");
    expect(chip()!.dataset.older).toBe("true");
    act(() => chip()!.click());
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([{ type: "protoArtifact:select", id: "a-1", ver: 2 }]);
  });

  it("a deleted/evicted artifact renders inert — no button, nothing opens", async () => {
    meta(null);
    await mount(REF);
    expect(chip()).toBeNull();
    const inert = container.querySelector('[data-testid="artifact-ref-gone"]');
    expect(inert?.textContent).toContain("no longer available");
  });

  it("a version trimmed at the cap renders inert", async () => {
    meta({ version_count: 60, oldest: 11 });
    await mount(REF);
    expect(chip()).toBeNull();
    expect(container.textContent).toContain("v2 of 60");
    expect(container.textContent).toContain("no longer kept");
  });

  it("with the Artifact panel off it's inert and never fetches", async () => {
    const spy = meta({ version_count: 2, oldest: 1 });
    useUI.setState({ railOrder: { left: ["chat"], right: [], bottom: [], hidden: [] } });
    await mount(REF);
    expect(chip()).toBeNull();
    expect(container.querySelector('[data-testid="artifact-ref-off"]')).not.toBeNull();
    expect(spy).not.toHaveBeenCalled();
  });

  it("a failing metadata route leaves it clickable (unknown state)", async () => {
    vi.spyOn(api, "artifactRefs").mockRejectedValue(new Error("404"));
    await mount(REF);
    expect(chip()!.textContent).toContain("v2");
  });

  it("renders a model-authored title as text, never markup", async () => {
    meta({ version_count: 2, oldest: 1 });
    await mount({ ...REF, title: '<img src=x onerror="window.__pwned=1">' });
    expect(container.querySelector("img")).toBeNull();
    expect(chip()!.textContent).toContain("<img src=x");
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("garbage props degrade to a labeled note", async () => {
    meta(null);
    await mount({ artifact_id: "", version: "x" });
    expect(container.textContent).toContain("[artifact-ref: missing artifact_id/version]");
  });

  // ── inline placement (ADR 0118 D2 / S7b) ──────────────────────────────────────────────────

  const frame = () => container.querySelector<HTMLIFrameElement>('[data-testid="artifact-inline-frame"]');

  it("a ref WITHOUT inline renders the unchanged chip — no embed frame", async () => {
    meta({ version_count: 2, oldest: 1 });
    await mount(REF);
    expect(chip()).not.toBeNull();
    expect(container.querySelector('[data-testid="artifact-ref-inline"]')).toBeNull();
    expect(frame()).toBeNull();
  });

  it("an inline ref hosts the artifact's embed frame and clamps its reported height to [80,1200]", async () => {
    meta({ version_count: 2, oldest: 1 });
    await mount({ ...REF, inline: true, height: 300 });
    // The chip button gives way to the embed frame; the head still offers Open in panel.
    expect(chip()).toBeNull();
    const f = frame();
    expect(f).not.toBeNull();
    expect(container.querySelector('[data-testid="artifact-inline-open"]')).not.toBeNull();
    // The embed URL names the artifact + version (S5 ?embed=<id>&v=<n>).
    expect(f!.getAttribute("src")).toContain("embed=a-1");
    expect(f!.getAttribute("src")).toContain("v=2");
    // The height hint seeds the initial size before the frame measures.
    expect(f!.style.height).toBe("300px");
    // A too-tall report clamps to the 1200 ceiling — taller content scrolls inside the frame.
    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", { data: { type: "protoArtifact:height", height: 5000 }, source: f!.contentWindow }),
      );
    });
    expect(f!.style.height).toBe("1200px");
    // …and a tiny report floors at 80.
    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", { data: { type: "protoArtifact:height", height: 12 }, source: f!.contentWindow }),
      );
    });
    expect(f!.style.height).toBe("80px");
    // A height post from some OTHER window is ignored (source is the strong gate).
    await act(async () => {
      window.dispatchEvent(new MessageEvent("message", { data: { type: "protoArtifact:height", height: 999 }, source: window }));
    });
    expect(f!.style.height).toBe("80px");
  });

  it("mounts the inline frame lazily — only once it scrolls near the viewport", async () => {
    meta({ version_count: 2, oldest: 1 });
    const callbacks: Array<(entries: Array<{ isIntersecting: boolean }>) => void> = [];
    class FakeIO {
      constructor(cb: (entries: Array<{ isIntersecting: boolean }>) => void) {
        callbacks.push(cb);
      }
      observe() {}
      disconnect() {}
    }
    (globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = FakeIO;
    try {
      await mount({ ...REF, inline: true });
      // Not yet scrolled in: the host card exists, but the frame hasn't mounted.
      expect(container.querySelector('[data-testid="artifact-ref-inline"]')).not.toBeNull();
      expect(frame()).toBeNull();
      // Scroll it into view → the frame mounts.
      await act(async () => {
        callbacks.forEach((cb) => cb([{ isIntersecting: true }]));
      });
      expect(frame()).not.toBeNull();
    } finally {
      (globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = undefined;
    }
  });

  it("obeys the live-frame cap: an evicted frame becomes a 'Click to resume' card keeping its height", async () => {
    vi.spyOn(api, "artifactRefs").mockImplementation(async (ids: string[]) => {
      const artifacts: Record<string, { title: string; kind: string; version_count: number; oldest: number }> = {};
      for (const id of ids) artifacts[id] = { title: "Chart", kind: "html", version_count: 1, oldest: 1 };
      return { artifacts };
    });
    const host = createInlineFrameHost(2); // a tiny cap so three frames exercise one eviction
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const ids = ["a-1", "a-2", "a-3"];
    act(() =>
      root.render(
        h(
          QueryClientProvider,
          { client: qc },
          h(
            InlineFrameHostContext.Provider,
            { value: host },
            ids.map((id) => h(ArtifactRefChip, { key: id, props: { artifact_id: id, version: 1, title: "Chart", kind: "html", inline: true, height: 240 } })),
          ),
        ),
      ),
    );
    await flush();
    // Three inline hosts, but the cap keeps only two frames live; the least-recently-visible
    // (the first mounted) is swapped for a resume card at its last height.
    expect(container.querySelectorAll('[data-testid="artifact-ref-inline"]').length).toBe(3);
    expect(container.querySelectorAll('[data-testid="artifact-inline-frame"]').length).toBe(2);
    const resume = container.querySelector<HTMLButtonElement>('[data-testid="artifact-inline-resume"]');
    expect(resume).not.toBeNull();
    expect(resume!.textContent).toContain("Click to resume");
    expect(resume!.style.height).toBe("240px"); // keeps its height so the layout doesn't jump
    // Resuming re-registers it: it mounts again and a different frame is evicted to hold the cap.
    await act(async () => {
      resume!.click();
    });
    await flush();
    expect(container.querySelectorAll('[data-testid="artifact-inline-frame"]').length).toBe(2);
    expect(container.querySelectorAll('[data-testid="artifact-inline-resume"]').length).toBe(1);
  });

  // The backend re-emits an inline ref for EVERY version of an inline artifact
  // (update_artifact/rewrite_artifact keep the inline placement), so several version frames of
  // one artifact id coexist in the transcript. The live-frame registry must key by (id, version),
  // not id alone — else the versions collapse into one slot and the cap stops holding.
  const metaMany = (version_count: number) =>
    vi.spyOn(api, "artifactRefs").mockImplementation(async (ids: string[]) => {
      const artifacts: Record<string, { title: string; kind: string; version_count: number; oldest: number }> = {};
      for (const id of ids) artifacts[id] = { title: "Chart", kind: "html", version_count, oldest: 1 };
      return { artifacts };
    });

  const renderInline = (versions: number[], host: ReturnType<typeof createInlineFrameHost>) => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    act(() =>
      root.render(
        h(
          QueryClientProvider,
          { client: qc },
          h(
            InlineFrameHostContext.Provider,
            { value: host },
            // The SAME artifact id at different versions — what a revised inline answer emits.
            versions.map((v) =>
              h(ArtifactRefChip, { key: v, props: { artifact_id: "a-1", version: v, title: "Chart", kind: "html", inline: true, height: 240 } }),
            ),
          ),
        ),
      ),
    );
  };

  it("the live-frame cap counts each VERSION of one artifact separately — siblings don't share a slot", async () => {
    metaMany(3);
    const host = createInlineFrameHost(2); // cap 2, so a third version frame forces one eviction
    renderInline([1, 2, 3], host);
    await flush();
    // Three inline hosts of the one artifact; keyed by id alone they'd all share a slot and the
    // cap wouldn't bite (three live frames). Keyed by (id, version) the cap holds: two frames, one
    // resume card — and the two live frames point at distinct versions.
    expect(container.querySelectorAll('[data-testid="artifact-ref-inline"]').length).toBe(3);
    expect(container.querySelectorAll('[data-testid="artifact-inline-frame"]').length).toBe(2);
    expect(container.querySelectorAll('[data-testid="artifact-inline-resume"]').length).toBe(1);
    const srcs = [...container.querySelectorAll('[data-testid="artifact-inline-frame"]')].map((f) => f.getAttribute("src"));
    expect(new Set(srcs).size).toBe(2);
  });

  it("unmounting one version's host leaves its sibling versions' frames live — release is per (id, version)", async () => {
    metaMany(2);
    const host = createInlineFrameHost(6); // room for both, so neither is evicted
    renderInline([1, 2], host);
    await flush();
    expect(container.querySelectorAll('[data-testid="artifact-inline-frame"]').length).toBe(2);
    // Drop version 1's host. Keyed by id alone, its release would delete the shared slot and flip
    // version 2 to a resume card; keyed by (id, version) it only frees its own slot.
    renderInline([2], host);
    await flush();
    expect(container.querySelectorAll('[data-testid="artifact-ref-inline"]').length).toBe(1);
    expect(container.querySelectorAll('[data-testid="artifact-inline-frame"]').length).toBe(1);
    expect(container.querySelectorAll('[data-testid="artifact-inline-resume"]').length).toBe(0);
  });

  it("an inline ref whose artifact is gone falls back to the inert chip — no frame", async () => {
    meta(null);
    await mount({ ...REF, inline: true });
    expect(frame()).toBeNull();
    expect(container.querySelector('[data-testid="artifact-ref-inline"]')).toBeNull();
    const inert = container.querySelector('[data-testid="artifact-ref-gone"]');
    expect(inert?.textContent).toContain("no longer available");
  });

  it("an inline ref with the Artifact panel off renders the inert chip — no frame", async () => {
    meta({ version_count: 2, oldest: 1 });
    useUI.setState({ railOrder: { left: ["chat"], right: [], bottom: [], hidden: [] } });
    await mount({ ...REF, inline: true });
    expect(frame()).toBeNull();
    expect(container.querySelector('[data-testid="artifact-ref-off"]')).not.toBeNull();
  });
});

// The send-to-chat / openLink bridge (ADR 0118 D4 / S10b): the inline host relays an in-frame
// protoArtifact.send()/openLink() request UP from ITS embed frame, runs it through the frameBridge
// gates, and posts the verdict (protoArtifact:bridgeResult) back down. Trust is the host's —
// never the model-authored frame — so these drive the gates end to end through the host.
describe("ArtifactRefChip — send/openLink bridge (ADR 0118 D4 / S10b)", () => {
  const frame = () => container.querySelector<HTMLIFrameElement>('[data-testid="artifact-inline-frame"]');

  function setActivation(value: { isActive: boolean } | undefined) {
    if (value === undefined) {
      delete (navigator as unknown as { userActivation?: unknown }).userActivation;
    } else {
      Object.defineProperty(navigator, "userActivation", { value, configurable: true });
    }
  }
  afterEach(() => setActivation(undefined));

  function chatStub(over: Partial<ArtifactChatSend> = {}): ArtifactChatSend & { send: ReturnType<typeof vi.fn> } {
    return { sessionId: "s-1", isBusy: () => false, send: vi.fn(), ...over } as ArtifactChatSend & {
      send: ReturnType<typeof vi.fn>;
    };
  }

  async function mountInline(chat: ArtifactChatSend | null, props: Record<string, unknown> = { ...REF, inline: true }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // A fresh bridge per mount so the module default's rate window from one test can't
    // rate-limit the next (the busy/rate gates precede the gesture gate).
    act(() =>
      root.render(
        h(
          QueryClientProvider,
          { client: qc },
          h(
            InlineFrameBridgeContext.Provider,
            { value: createFrameBridge() },
            h(ArtifactChatSendContext.Provider, { value: chat }, h(ArtifactRefChip, { props })),
          ),
        ),
      ),
    );
    await flush();
    const f = frame()!;
    expect(f).not.toBeNull();
    // The host posts its verdict back to the frame's own window — capture it rather than let
    // jsdom actually dispatch into the detached contentWindow.
    const post = vi.spyOn(f.contentWindow as Window, "postMessage").mockImplementation(() => {});
    return { f, post };
  }

  async function dispatch(f: HTMLIFrameElement, data: Record<string, unknown>) {
    await act(async () => {
      window.dispatchEvent(new MessageEvent("message", { data, source: f.contentWindow }));
    });
  }

  const bridgeResult = (ok: boolean, extra: Record<string, unknown> = {}) =>
    [expect.objectContaining({ type: "protoArtifact:bridgeResult", cid: 7, ok, ...extra }), "*"];

  it("a gesture-backed send posts a labelled user turn and resolves the frame's promise", async () => {
    meta({ version_count: 2, oldest: 1 });
    setActivation({ isActive: true });
    const chat = chatStub();
    const { f, post } = await mountInline(chat);
    await dispatch(f, { type: "protoArtifact:send", cid: 7, text: "  Recompute at 42  ", artifact_id: "a-1", version: 2 });
    // The NORMAL send path is used, tagged with the D4 origin metadata + title for the label.
    expect(chat.send).toHaveBeenCalledTimes(1);
    expect(chat.send).toHaveBeenCalledWith("Recompute at 42", { artifact_id: "a-1", version: 2, title: "Chart" });
    expect(post).toHaveBeenCalledWith(...bridgeResult(true, { text: "Recompute at 42" }));
    expect(container.querySelector('[data-testid="artifact-send-rejected"]')).toBeNull();
  });

  it("a send with NO user activation is rejected — no turn starts", async () => {
    meta({ version_count: 2, oldest: 1 });
    setActivation({ isActive: false });
    const chat = chatStub();
    const { f, post } = await mountInline(chat);
    await dispatch(f, { type: "protoArtifact:send", cid: 7, text: "do it", artifact_id: "a-1", version: 2 });
    expect(chat.send).not.toHaveBeenCalled();
    // The frame learns why, and the operator sees the refusal in place.
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("click or key press") }));
    const notice = container.querySelector('[data-testid="artifact-send-rejected"]');
    expect(notice?.textContent).toContain("click or key press");
  });

  it("a send while the agent is busy is rejected with 'the agent is busy' — no turn starts", async () => {
    meta({ version_count: 2, oldest: 1 });
    setActivation({ isActive: true });
    const chat = chatStub({ isBusy: () => true });
    const { f, post } = await mountInline(chat);
    await dispatch(f, { type: "protoArtifact:send", cid: 7, text: "do it", artifact_id: "a-1", version: 2 });
    expect(chat.send).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: "the agent is busy" }));
    expect(container.querySelector('[data-testid="artifact-send-rejected"]')?.textContent).toContain("the agent is busy");
  });

  it("with no User Activation API it asks first, then posts the turn when the operator confirms", async () => {
    meta({ version_count: 2, oldest: 1 });
    setActivation(undefined); // runtime without navigator.userActivation
    const chat = chatStub();
    const { f, post } = await mountInline(chat);
    await dispatch(f, { type: "protoArtifact:send", cid: 7, text: "run it", artifact_id: "a-1", version: 2 });
    // Nothing posted yet — the host asks inline.
    expect(chat.send).not.toHaveBeenCalled();
    const confirm = container.querySelector('[data-testid="artifact-send-confirm"]');
    expect(confirm?.textContent).toContain('Send "run it" to chat?');
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[data-testid="artifact-send-confirm-ok"]')!.click();
    });
    expect(chat.send).toHaveBeenCalledWith("run it", { artifact_id: "a-1", version: 2, title: "Chart" });
    expect(post).toHaveBeenCalledWith(...bridgeResult(true, { text: "run it" }));
  });

  it("cancelling the confirm prompt sends nothing and rejects the frame's promise", async () => {
    meta({ version_count: 2, oldest: 1 });
    setActivation(undefined);
    const chat = chatStub();
    const { f, post } = await mountInline(chat);
    await dispatch(f, { type: "protoArtifact:send", cid: 7, text: "run it", artifact_id: "a-1", version: 2 });
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[data-testid="artifact-send-confirm-cancel"]')!.click();
    });
    expect(chat.send).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("cancelled") }));
  });

  it("with no chat wired the send is refused rather than left hanging", async () => {
    meta({ version_count: 2, oldest: 1 });
    setActivation({ isActive: true });
    const { f, post } = await mountInline(null);
    await dispatch(f, { type: "protoArtifact:send", cid: 7, text: "do it", artifact_id: "a-1", version: 2 });
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("can't send to chat") }));
  });

  it("a send from SOME OTHER window is ignored (e.source is the gate)", async () => {
    meta({ version_count: 2, oldest: 1 });
    setActivation({ isActive: true });
    const chat = chatStub();
    await mountInline(chat);
    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", { data: { type: "protoArtifact:send", cid: 7, text: "do it" }, source: window }),
      );
    });
    expect(chat.send).not.toHaveBeenCalled();
  });

  it("openLink opens an https link in a new tab with noopener,noreferrer, through the host", async () => {
    meta({ version_count: 2, oldest: 1 });
    const chat = chatStub();
    const { f, post } = await mountInline(chat);
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    await dispatch(f, { type: "protoArtifact:openLink", cid: 7, url: "https://example.com/docs" });
    expect(open).toHaveBeenCalledWith("https://example.com/docs", "_blank", "noopener,noreferrer");
    expect(post).toHaveBeenCalledWith(...bridgeResult(true));
  });

  it("openLink refuses a non-https link and opens nothing", async () => {
    meta({ version_count: 2, oldest: 1 });
    const chat = chatStub();
    const { f, post } = await mountInline(chat);
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    await dispatch(f, { type: "protoArtifact:openLink", cid: 7, url: "http://example.com" });
    expect(open).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("https") }));
    expect(container.querySelector('[data-testid="artifact-send-rejected"]')?.textContent).toContain("https");
  });
});
