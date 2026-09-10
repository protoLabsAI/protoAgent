// Structured setup-gap banners (graph/plugins/setup_gaps.py rendered in the shell strip):
// legacy string warnings keep rendering elsewhere, this suite covers the STRUCTURED path —
// CTA navigation into the plugin-config dialog, action safety for unknown/malformed kinds,
// and the session-scoped dismissal lifecycle. createRoot/act + the real uiStore, like the
// other console UI suites (FleetRoom.test.tsx) — no testing-library dep.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useUI } from "../state/uiStore";
import {
  SetupGapBanner,
  gapIdentity,
  gapSignature,
  isSetupGap,
  useSetupGapDismissals,
  type SetupGap,
} from "./SetupGapBanner";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function pbGap(over: Partial<SetupGap> = {}): SetupGap {
  return {
    plugin: "projectBoard",
    label: "Project Board",
    key: "coder",
    message: "No coder delegate is configured, so the board can't run features.",
    actions: [{ kind: "plugin_config", target: "projectBoard", label: "Configure Project Board" }],
    ...over,
  };
}

let container: HTMLElement;
let root: Root;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  window.sessionStorage.clear();
  // Ephemeral overlay state the CTAs drive — reset so assertions read this test's write.
  useUI.setState({ configurePlugin: undefined, globalSettingsOpen: false, globalSettingsSection: undefined });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  window.sessionStorage.clear();
});

const buttons = () => Array.from(container.querySelectorAll("button"));
const buttonByText = (text: string) => buttons().find((b) => (b.textContent || "").trim() === text);
const dismissButton = () => container.querySelector<HTMLButtonElement>('[data-testid="setup-gap-dismiss"]');
// Any CTA affordance (a Configure / Open settings button) — the interactive control r3 says
// an unknown or malformed action must NOT produce. The dismiss control is separate (testid).
const ctaButtons = () =>
  buttons().filter((b) => /Configure|Open settings/.test((b.textContent || "").trim()));

describe("isSetupGap — splits structured gaps from legacy strings", () => {
  it("accepts a well-formed gap and rejects strings / malformed objects", () => {
    expect(isSetupGap(pbGap())).toBe(true);
    expect(isSetupGap("Another running instance shares this data root")).toBe(false);
    expect(isSetupGap({ plugin: "x" })).toBe(false); // missing key/message/label
    expect(isSetupGap(null)).toBe(false);
  });
});

describe("SetupGapBanner — rendering + CTA navigation", () => {
  it("renders the plugin label + message as a warning alert", () => {
    act(() => root.render(h(SetupGapBanner, { gap: pbGap(), onDismiss: () => {} })));
    const banner = container.querySelector(".setup-gap-banner");
    expect(banner).not.toBeNull();
    expect(banner?.getAttribute("role")).toBe("alert");
    expect(container.textContent).toContain("Project Board");
    expect(container.textContent).toContain("No coder delegate is configured");
  });

  it("opens the plugin-config dialog for the reporting plugin on the allowlisted plugin_config action", () => {
    act(() => root.render(h(SetupGapBanner, { gap: pbGap({ key: "coder" }), onDismiss: () => {} })));
    const cta = buttonByText("Configure Project Board");
    expect(cta).toBeTruthy();
    act(() => cta!.click());
    // Routed through the existing useUI.openPluginConfig(pluginId, label) path.
    expect(useUI.getState().configurePlugin).toEqual({ id: "projectBoard", name: "Project Board" });
  });

  it("also renders the CTA for a `repo` gap (same Configure Project Board affordance)", () => {
    act(() =>
      root.render(
        h(SetupGapBanner, { gap: pbGap({ key: "repo", message: "No repository is bound." }), onDismiss: () => {} }),
      ),
    );
    expect(buttonByText("Configure Project Board")).toBeTruthy();
  });

  it("maps a global_settings action to the global-settings overlay at its target section", () => {
    const gap = pbGap({
      plugin: "core",
      label: "Scheduler",
      key: "disabled",
      actions: [{ kind: "global_settings", target: "telemetry", label: "Open settings" }],
    });
    act(() => root.render(h(SetupGapBanner, { gap, onDismiss: () => {} })));
    act(() => buttonByText("Open settings")!.click());
    expect(useUI.getState().globalSettingsOpen).toBe(true);
    expect(useUI.getState().globalSettingsSection).toBe("telemetry");
  });
});

describe("SetupGapBanner — action safety (r3)", () => {
  it("renders NO interactive control for an unknown action kind, but keeps the message", () => {
    const gap = pbGap({ actions: [{ kind: "open_url", target: "https://evil.example" }] });
    act(() => root.render(h(SetupGapBanner, { gap, onDismiss: () => {} })));
    // No CTA affordance, and nothing became a link — the dismiss control is still present.
    expect(ctaButtons()).toHaveLength(0);
    expect(container.querySelector("a")).toBeNull();
    expect(dismissButton()).not.toBeNull();
    expect(container.textContent).toContain("No coder delegate is configured");
  });

  it("degrades to message + dismiss when actions is absent or malformed", () => {
    act(() => root.render(h(SetupGapBanner, { gap: pbGap({ actions: undefined }), onDismiss: () => {} })));
    expect(ctaButtons()).toHaveLength(0);
    expect(dismissButton()).not.toBeNull();
    act(() => root.render(h(SetupGapBanner, { gap: pbGap({ actions: "nope" as never }), onDismiss: () => {} })));
    expect(ctaButtons()).toHaveLength(0);
    expect(dismissButton()).not.toBeNull();
  });

  it("skips a malformed entry but still renders a valid sibling action", () => {
    const gap = pbGap({
      actions: [
        { kind: "mystery" },
        { kind: "plugin_config", label: "Configure Project Board" },
      ],
    });
    act(() => root.render(h(SetupGapBanner, { gap, onDismiss: () => {} })));
    expect(buttonByText("Configure Project Board")).toBeTruthy();
    expect(ctaButtons()).toHaveLength(1); // the one valid CTA; the malformed entry produced none
  });
});

describe("SetupGapBanner — dismiss control", () => {
  it("invokes onDismiss when the dismiss button is clicked", () => {
    let dismissed = 0;
    act(() => root.render(h(SetupGapBanner, { gap: pbGap(), onDismiss: () => { dismissed += 1; } })));
    const btn = dismissButton();
    expect(btn?.getAttribute("aria-label")).toContain("Dismiss");
    act(() => btn!.click());
    expect(dismissed).toBe(1);
  });
});

// A tiny harness so the hook can be exercised through a real render tree.
function GapList({ gaps }: { gaps: SetupGap[] }) {
  const { visibleGaps, dismiss } = useSetupGapDismissals(gaps);
  return h(
    "div",
    null,
    visibleGaps.map((g) =>
      h(
        "div",
        { key: gapIdentity(g), "data-testid": "visible-gap", "data-id": gapIdentity(g) },
        h("button", { "data-testid": `dismiss-${g.plugin}-${g.key}`, onClick: () => dismiss(g) }, "x"),
      ),
    ),
  );
}

const visibleIds = () =>
  Array.from(container.querySelectorAll('[data-testid="visible-gap"]')).map((el) => el.getAttribute("data-id"));

describe("useSetupGapDismissals — session-scoped dismissal lifecycle (r4)", () => {
  it("hides only the dismissed gap and leaves the others", async () => {
    const a = pbGap({ key: "coder" });
    const b = pbGap({ key: "repo", message: "No repository is bound." });
    act(() => root.render(h(GapList, { gaps: [a, b] })));
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(a), gapIdentity(b)]);

    act(() => container.querySelector<HTMLButtonElement>('[data-testid="dismiss-projectBoard-coder"]')!.click());
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(b)]);
  });

  it("keeps the dismissal for the rest of the session (survives a fresh mount)", async () => {
    const a = pbGap();
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    act(() => container.querySelector<HTMLButtonElement>('[data-testid="dismiss-projectBoard-coder"]')!.click());
    await flush();
    expect(visibleIds()).toEqual([]);

    // Tear down and mount a FRESH tree — its initial state reads the dismissal back out of
    // sessionStorage (same session), so the gap stays hidden.
    act(() => root.unmount());
    root = createRoot(container);
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    expect(visibleIds()).toEqual([]);
  });

  it("returns on a NEW browser session (sessionStorage cleared)", async () => {
    const a = pbGap();
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    act(() => container.querySelector<HTMLButtonElement>('[data-testid="dismiss-projectBoard-coder"]')!.click());
    await flush();
    expect(visibleIds()).toEqual([]);

    // New session: clear sessionStorage AND mount a fresh tree, so nothing is remembered.
    act(() => root.unmount());
    window.sessionStorage.clear();
    root = createRoot(container);
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(a)]);
  });

  it("resets the dismissal when the gap's message changes (signature moves)", async () => {
    const before = pbGap({ message: "No coder delegate is configured." });
    act(() => root.render(h(GapList, { gaps: [before] })));
    await flush();
    act(() => container.querySelector<HTMLButtonElement>('[data-testid="dismiss-projectBoard-coder"]')!.click());
    await flush();
    expect(visibleIds()).toEqual([]);

    // Same (plugin,key) identity, changed message → different signature → shows again.
    const after = pbGap({ message: "Two coder delegates conflict; pick one." });
    expect(gapSignature(after)).not.toBe(gapSignature(before));
    act(() => root.render(h(GapList, { gaps: [after] })));
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(after)]);
  });

  it("stops tracking a dismissal once the gap clears (prunes stale storage)", async () => {
    const a = pbGap();
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    act(() => container.querySelector<HTMLButtonElement>('[data-testid="dismiss-projectBoard-coder"]')!.click());
    await flush();
    expect(JSON.parse(window.sessionStorage.getItem("protoagent.setupGapDismissals") || "[]")).toHaveLength(1);

    // Server clears the gap → its stale dismissal signature is pruned from storage.
    act(() => root.render(h(GapList, { gaps: [] })));
    await flush();
    expect(JSON.parse(window.sessionStorage.getItem("protoagent.setupGapDismissals") || "[]")).toHaveLength(0);
  });
});
