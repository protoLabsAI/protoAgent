import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chatStore } from "../chat/chat-store";
import { dispatchLiveComponent } from "../ext/componentRegistry";
import { resetPluginViewInbox, takePluginViewMessages } from "../lib/pluginViewInbox";
import { useUI } from "../state/uiStore";
import {
  ARTIFACT_VIEW_KEY,
  artifactRefFromProps,
  onLiveArtifactRef,
  openArtifactRef,
  refName,
  refState,
  versionLabel,
} from "./artifactRef";
import "../ext/artifact"; // registers the chip + its live hook, as the ext glob does at boot

// The artifact-ref pointer (#3617): prop parsing, the "v2 of 5" state, and the open rules —
// a click always opens; the LIVE turn auto-opens only on desktop, for the chat on screen, and
// never onto chat's own dock.

function setMobile(on: boolean) {
  window.matchMedia = ((q: string) => ({ matches: on && q.includes("max-width"), media: q })) as never;
}

const baseRail = () => ({ left: ["chat"], right: [ARTIFACT_VIEW_KEY], bottom: [] as string[], hidden: [] as string[] });

beforeEach(() => {
  resetPluginViewInbox();
  setMobile(false);
  useUI.setState({ railOrder: baseRail(), rightCollapsed: true, rightPanel: "notes" });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("artifactRefFromProps", () => {
  it("parses a valid ref and rejects missing/garbage ids and versions", () => {
    expect(artifactRefFromProps({ artifact_id: "a-1", version: 2, title: " My\n page ", kind: "html" })).toEqual({
      id: "a-1",
      version: 2,
      title: "My page",
      kind: "html",
      inline: false,
      height: 0,
    });
    expect(artifactRefFromProps({ artifact_id: "", version: 1 })).toBeNull();
    expect(artifactRefFromProps({ artifact_id: "a", version: 0 })).toBeNull();
    expect(artifactRefFromProps({ artifact_id: "a", version: 1.5 })).toBeNull();
    expect(artifactRefFromProps({ artifact_id: "a", version: "2" })).toBeNull();
    expect(artifactRefFromProps(undefined)).toBeNull();
    // An unknown kind is dropped rather than echoed into the chip.
    expect(artifactRefFromProps({ artifact_id: "a", version: 1, kind: "<b>x</b>" })?.kind).toBe("");
    // A chart (ADR 0116) is a known kind, so an untitled one still says what it is.
    expect(artifactRefFromProps({ artifact_id: "a", version: 1, kind: "vega-lite" })?.kind).toBe("vega-lite");
  });

  it("names an untitled artifact by its kind", () => {
    expect(refName({ title: "", kind: "svg" })).toBe("svg artifact");
    expect(refName({ title: "", kind: "" })).toBe("Artifact");
  });

  it("reads inline + a clamped height hint off untrusted props (ADR 0118 D2)", () => {
    // `inline` is an exact boolean — a truthy non-true value does not opt in.
    expect(artifactRefFromProps({ artifact_id: "a", version: 1, inline: true })).toMatchObject({ inline: true });
    expect(artifactRefFromProps({ artifact_id: "a", version: 1, inline: 1 as unknown })).toMatchObject({ inline: false });
    expect(artifactRefFromProps({ artifact_id: "a", version: 1 })?.inline).toBe(false);
    // `height` is a finite positive int run through the [80,1200] clamp; junk/≤0 → 0 (no hint).
    expect(artifactRefFromProps({ artifact_id: "a", version: 1, inline: true, height: 300 })?.height).toBe(300);
    expect(artifactRefFromProps({ artifact_id: "a", version: 1, inline: true, height: 5000 })?.height).toBe(1200);
    expect(artifactRefFromProps({ artifact_id: "a", version: 1, inline: true, height: 10 })?.height).toBe(80);
    expect(artifactRefFromProps({ artifact_id: "a", version: 1, inline: true, height: -4 })?.height).toBe(0);
    expect(artifactRefFromProps({ artifact_id: "a", version: 1, inline: true, height: "big" as unknown })?.height).toBe(0);
  });
});

describe("refState / versionLabel", () => {
  it("latest, older, trimmed, gone and unknown", () => {
    expect(refState(5, { version_count: 5, oldest: 1 })).toEqual({ kind: "latest", total: 5 });
    expect(refState(2, { version_count: 5, oldest: 1 })).toEqual({ kind: "older", total: 5 });
    expect(refState(2, { version_count: 60, oldest: 11 })).toEqual({ kind: "trimmed", total: 60 });
    expect(refState(2, null)).toEqual({ kind: "gone" });
    expect(refState(2, undefined)).toEqual({ kind: "unknown" });
    expect(versionLabel(2, { kind: "older", total: 5 })).toBe("v2 of 5");
    expect(versionLabel(5, { kind: "latest", total: 5 })).toBe("v5");
    expect(versionLabel(3, { kind: "unknown" })).toBe("v3");
  });
});

describe("openArtifactRef", () => {
  it("a click queues the select and opens the panel (uncollapsing its dock)", () => {
    expect(openArtifactRef({ id: "a-1", version: 2 })).toBe(true);
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([{ type: "protoArtifact:select", id: "a-1", ver: 2 }]);
    const ui = useUI.getState();
    expect(ui.rightPanel).toBe(ARTIFACT_VIEW_KEY);
    expect(ui.rightCollapsed).toBe(false);
  });

  it("a click on a phone pushes the panel (mobileActive)", () => {
    setMobile(true);
    expect(openArtifactRef({ id: "a-1", version: 1 })).toBe(true);
    expect(useUI.getState().mobileActive).toBe(ARTIFACT_VIEW_KEY);
  });

  it("does nothing when the Artifact panel isn't a surface (plugin off)", () => {
    useUI.setState({ railOrder: { left: ["chat"], right: [], bottom: [], hidden: [] } });
    expect(openArtifactRef({ id: "a-1", version: 1 })).toBe(false);
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([]);
  });

  it("auto: never on a phone, never for a background tab, never onto chat's dock", () => {
    setMobile(true);
    expect(openArtifactRef({ id: "a", version: 1 }, { auto: true })).toBe(false);
    setMobile(false);
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ ...chatStore.getSnapshot(), currentSessionId: "s-front" });
    expect(openArtifactRef({ id: "a", version: 1 }, { auto: true, sessionId: "s-back" })).toBe(false);
    useUI.setState({ railOrder: { left: ["chat", ARTIFACT_VIEW_KEY], right: [], bottom: [], hidden: [] } });
    expect(openArtifactRef({ id: "a", version: 1 }, { auto: true, sessionId: "s-front" })).toBe(false);
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([]);
    expect(useUI.getState().rightCollapsed).toBe(true);
    // …but the operator's own click still opens it there.
    expect(openArtifactRef({ id: "a", version: 1 })).toBe(true);
  });

  it("auto: opens for the chat on screen", () => {
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ ...chatStore.getSnapshot(), currentSessionId: "s-1" });
    onLiveArtifactRef({ component: "artifact-ref", props: { artifact_id: "a-9", version: 3, kind: "svg" } }, { sessionId: "s-1" });
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([{ type: "protoArtifact:select", id: "a-9", ver: 3 }]);
    expect(useUI.getState().rightPanel).toBe(ARTIFACT_VIEW_KEY);
  });

  it("auto: does NOT open the panel for an inline ref — the answer is already in the transcript", () => {
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ ...chatStore.getSnapshot(), currentSessionId: "s-1" });
    onLiveArtifactRef(
      { component: "artifact-ref", props: { artifact_id: "a-inline", version: 1, kind: "html", inline: true } },
      { sessionId: "s-1" },
    );
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([]);
    expect(useUI.getState().rightCollapsed).toBe(true);
    // The same ref WITHOUT inline still auto-opens, so this is the inline gate, not a dead hook.
    onLiveArtifactRef(
      { component: "artifact-ref", props: { artifact_id: "a-inline", version: 1, kind: "html" } },
      { sessionId: "s-1" },
    );
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([{ type: "protoArtifact:select", id: "a-inline", ver: 1 }]);
  });

  it("the ext module wires the live hook through the component registry", () => {
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ ...chatStore.getSnapshot(), currentSessionId: "s-1" });
    dispatchLiveComponent({ component: "artifact-ref", props: { artifact_id: "a-2", version: 1 } }, "s-1");
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([{ type: "protoArtifact:select", id: "a-2", ver: 1 }]);
    // An unrelated kind's live frame touches nothing.
    dispatchLiveComponent({ component: "table", props: {} }, "s-1");
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([]);
  });
});
