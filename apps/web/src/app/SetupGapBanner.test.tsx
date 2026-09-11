// Structured setup-gap banners (graph/plugins/setup_gaps.py rendered in the shell strip):
// how runtime status splits into banners vs plain alerts, CTA navigation into the
// plugin-config dialog, action safety for unknown/malformed kinds, and the session-scoped
// dismissal lifecycle. createRoot/act + the real uiStore, like the other console UI suites
// (FleetRoom.test.tsx) — no testing-library dep.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

// The REAL runtime-status payload for two reported gaps — asserted against the live handler by
// tests/test_console_handlers.py, and served by the e2e (see the file's `_about`).
import golden from "../../e2e/setup-gaps.golden.json";
import { useUI } from "../state/uiStore";
import {
  SetupGapBanner,
  gapIdentity,
  gapSignature,
  gapWarningLine,
  isSetupGap,
  splitRuntimeWarnings,
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

describe("isSetupGap — well-formed structured gaps only", () => {
  it("accepts a well-formed gap and rejects strings / malformed objects", () => {
    expect(isSetupGap(pbGap())).toBe(true);
    expect(isSetupGap("Another running instance shares this data root")).toBe(false);
    expect(isSetupGap({ plugin: "x" })).toBe(false); // missing key/message/label
    expect(isSetupGap(null)).toBe(false);
  });
});

// The runtime-status contract (#3395): each gap arrives as a record in `setup_gaps[]` AND as its
// `Label: message` line in `warnings[]`. The console used to build banners only from objects
// inside `warnings[]` — a shape the server never sends — so every real gap rendered as a plain
// alert with no Configure button and no dismiss (QA of v0.164.0).
describe("splitRuntimeWarnings — the real runtime-status shape", () => {
  const OTHER = "Another running instance shares this agent's data.";

  it("turns the real server payload into one banner per gap and no leftover plain lines", () => {
    const { plainWarnings, setupGaps } = splitRuntimeWarnings(golden.status);
    expect(setupGaps.map(gapIdentity)).toEqual(["boardy coder", "boardy repo"]);
    expect(plainWarnings).toEqual([]); // both lines were the gaps' own projection
  });

  it("keeps genuinely plain operational warnings, in order, beside the gaps", () => {
    const status = { warnings: [OTHER, ...golden.status.warnings], setup_gaps: golden.status.setup_gaps };
    const { plainWarnings, setupGaps } = splitRuntimeWarnings(status);
    expect(plainWarnings).toEqual([OTHER]);
    expect(setupGaps).toHaveLength(2);
  });

  it("drops a gap's line by its EXACT projection — a warning that merely mentions the plugin survives", () => {
    const gap = pbGap();
    expect(gapWarningLine(gap)).toBe("Project Board: No coder delegate is configured, so the board can't run features.");
    const near = "Project Board: the board's coder delegate crashed.";
    const { plainWarnings } = splitRuntimeWarnings({ warnings: [near, gapWarningLine(gap)], setup_gaps: [gap] });
    expect(plainWarnings).toEqual([near]);
  });

  it("a server without `setup_gaps` (pre-#3395) keeps every line as a plain warning", () => {
    const { plainWarnings, setupGaps } = splitRuntimeWarnings({ warnings: golden.status.warnings });
    expect(setupGaps).toEqual([]);
    expect(plainWarnings).toEqual(golden.status.warnings);
  });

  it("a malformed `setup_gaps` entry is dropped, and its line stays visible as a plain alert", () => {
    const line = "Acme: gh is not authenticated";
    const { plainWarnings, setupGaps } = splitRuntimeWarnings({
      warnings: [line],
      setup_gaps: [{ plugin: "acme", message: "gh is not authenticated" }], // no key/label
    });
    expect(setupGaps).toEqual([]);
    expect(plainWarnings).toEqual([line]);
  });

  it("ignores gap-shaped OBJECTS inside `warnings[]` — never a server shape", () => {
    const { plainWarnings, setupGaps } = splitRuntimeWarnings({ warnings: [OTHER, pbGap()] as unknown[] });
    expect(setupGaps).toEqual([]);
    expect(plainWarnings).toEqual([OTHER]);
  });

  it("renders nothing for an absent / empty status", () => {
    for (const status of [undefined, null, { warnings: [], setup_gaps: [] }]) {
      const { plainWarnings, setupGaps } = splitRuntimeWarnings(status);
      expect(plainWarnings).toEqual([]);
      expect(setupGaps).toEqual([]);
    }
  });

  it("says whether the gap list is KNOWN — present (even empty) vs not loaded / pre-#3395", () => {
    // `setup_gaps` is always present on a current server (`[]` when there are none), so an
    // empty list is a REAL "no gaps" the dismissals may reset on; a missing status or field is
    // not — that's a reload before the status arrives, or an older server.
    expect(splitRuntimeWarnings({ warnings: [], setup_gaps: [] }).gapsKnown).toBe(true);
    expect(splitRuntimeWarnings(golden.status).gapsKnown).toBe(true);
    expect(splitRuntimeWarnings({ warnings: golden.status.warnings }).gapsKnown).toBe(false);
    expect(splitRuntimeWarnings(null).gapsKnown).toBe(false);
    expect(splitRuntimeWarnings(undefined).gapsKnown).toBe(false);
  });

  it("renders the real payload's gaps with the host-allowlisted CTA (and none where the host dropped it)", () => {
    const { setupGaps } = splitRuntimeWarnings(golden.status);
    const [coder, repo] = setupGaps;
    act(() => root.render(h(SetupGapBanner, { gap: coder, onDismiss: () => {} })));
    expect(buttonByText("Configure Project Board")).toBeTruthy();
    act(() => root.render(h(SetupGapBanner, { gap: repo, onDismiss: () => {} })));
    expect(ctaButtons()).toHaveLength(0); // its open_url action never survived the sanitizer
    expect(dismissButton()).not.toBeNull();
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

// A tiny harness so the hook can be exercised through a real render tree. `scope` is the
// focused agent (App passes `currentSlug()`); `authoritative` is "the status carried the server's
// `setup_gaps[]` list" (splitRuntimeWarnings' `gapsKnown`) — false while it isn't loaded yet or
// on a server that predates the field.
function GapList({ gaps, scope = "host", authoritative = true }: { gaps: SetupGap[]; scope?: string; authoritative?: boolean }) {
  const { visibleGaps, dismiss } = useSetupGapDismissals(gaps, { scope, authoritative });
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
/** One agent's stored dismissal signatures (sessionStorage, keyed per agent). */
const stored = (scope = "host"): string[] =>
  JSON.parse(window.sessionStorage.getItem(`protoagent.setupGapDismissals:${scope}`) || "[]");
const clickDismiss = (key: string) =>
  act(() => container.querySelector<HTMLButtonElement>(`[data-testid="dismiss-projectBoard-${key}"]`)!.click());
/** Mount a FRESH tree — a page load: the hook's initial state is read back out of storage. */
function remount() {
  act(() => root.unmount());
  root = createRoot(container);
}

describe("useSetupGapDismissals — session-scoped dismissal lifecycle (r4)", () => {
  it("hides only the dismissed gap and leaves the others", async () => {
    const a = pbGap({ key: "coder" });
    const b = pbGap({ key: "repo", message: "No repository is bound." });
    act(() => root.render(h(GapList, { gaps: [a, b] })));
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(a), gapIdentity(b)]);

    clickDismiss("coder");
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(b)]);
  });

  it("keeps the dismissal for the rest of the session (survives a fresh mount)", async () => {
    const a = pbGap();
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    clickDismiss("coder");
    await flush();
    expect(visibleIds()).toEqual([]);

    // A FRESH tree reads the dismissal back out of sessionStorage (same session), so the gap
    // stays hidden.
    remount();
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    expect(visibleIds()).toEqual([]);
  });

  it("returns on a NEW browser session (sessionStorage cleared)", async () => {
    const a = pbGap();
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    clickDismiss("coder");
    await flush();
    expect(visibleIds()).toEqual([]);

    // New session: clear sessionStorage AND mount a fresh tree, so nothing is remembered.
    remount();
    window.sessionStorage.clear();
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(a)]);
  });

  it("resets the dismissal when the gap's message changes (signature moves)", async () => {
    const before = pbGap({ message: "No coder delegate is configured." });
    act(() => root.render(h(GapList, { gaps: [before] })));
    await flush();
    clickDismiss("coder");
    await flush();
    expect(visibleIds()).toEqual([]);

    // Same (plugin,key) identity, changed message → different signature → shows again.
    const after = pbGap({ message: "Two coder delegates conflict; pick one." });
    expect(gapSignature(after)).not.toBe(gapSignature(before));
    act(() => root.render(h(GapList, { gaps: [after] })));
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(after)]);
  });

  it("resets on a REAL clear: the server's list is present and empty, so a recurring gap shows again", async () => {
    // #3421's stated contract — a dismissal "resets when the server clears the gap". The
    // operator fixed it (the server now sends `setup_gaps: []`); if it breaks again later with
    // the same text, that is a NEW problem, and it must not stay hidden for the session.
    const a = pbGap();
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    clickDismiss("coder");
    await flush();
    expect(stored()).toHaveLength(1);

    act(() => root.render(h(GapList, { gaps: [], authoritative: true })));
    await flush();
    expect(stored()).toEqual([]);

    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(a)]);
  });

  it("never resets while the gap list is UNKNOWN (status not loaded yet, or a pre-setup_gaps server)", async () => {
    const a = pbGap();
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    clickDismiss("coder");
    await flush();
    expect(stored()).toHaveLength(1);

    // A reload renders before the status arrives: no list to compare against, so no prune.
    act(() => root.render(h(GapList, { gaps: [], authoritative: false })));
    await flush();
    expect(stored()).toHaveLength(1);

    // The unchanged gap comes back → it stays hidden for the rest of the session.
    act(() => root.render(h(GapList, { gaps: [a] })));
    await flush();
    expect(visibleIds()).toEqual([]);
  });

  it("prunes a stale dismissal once its gap clears while another gap stays live", async () => {
    const a = pbGap({ key: "coder" });
    const b = pbGap({ key: "repo", message: "No repository is bound." });
    act(() => root.render(h(GapList, { gaps: [a, b] })));
    await flush();
    clickDismiss("coder");
    await flush();
    clickDismiss("repo");
    await flush();
    expect(stored()).toHaveLength(2);

    // `a` clears but `b` stays live → a's stale signature is pruned (storage hygiene) while b's
    // dismissal is retained.
    act(() => root.render(h(GapList, { gaps: [b] })));
    await flush();
    expect(stored()).toEqual([gapSignature(b)]);
  });
});

// Runtime status is the FOCUSED agent's, and switching agents is a full page load in the same
// tab (same sessionStorage). A dismissal is one agent's acknowledgement of one of ITS problems:
// it must neither hide another agent's identical gap nor be pruned by another agent's live set
// (#3438 review — both happened with one unscoped key).
describe("useSetupGapDismissals — scoped to the focused agent", () => {
  it("dismissing a gap on one agent never hides another agent's identical gap", async () => {
    const a = pbGap();
    act(() => root.render(h(GapList, { gaps: [a], scope: "host" })));
    await flush();
    clickDismiss("coder");
    await flush();
    expect(visibleIds()).toEqual([]);

    remount(); // focus the member: a full page load in the same tab
    act(() => root.render(h(GapList, { gaps: [a], scope: "ava" })));
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(a)]);
  });

  it("another agent's live gap set never prunes this agent's dismissals", async () => {
    const a = pbGap({ key: "coder" });
    const b = pbGap({ key: "repo", message: "No repository is bound." });
    act(() => root.render(h(GapList, { gaps: [a], scope: "host" })));
    await flush();
    clickDismiss("coder");
    await flush();

    remount();
    act(() => root.render(h(GapList, { gaps: [b], scope: "ava" }))); // a different, authoritative set
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(b)]);

    remount(); // back to the hub: its gap is unchanged and this is the same session
    act(() => root.render(h(GapList, { gaps: [a], scope: "host" })));
    await flush();
    expect(visibleIds()).toEqual([]);
    expect(stored("host")).toEqual([gapSignature(a)]);
  });

  it("a mounted hook that changes agent swaps to THAT agent's dismissals", async () => {
    const a = pbGap();
    act(() => root.render(h(GapList, { gaps: [a], scope: "host" })));
    await flush();
    clickDismiss("coder");
    await flush();

    act(() => root.render(h(GapList, { gaps: [a], scope: "ava" })));
    await flush();
    expect(visibleIds()).toEqual([gapIdentity(a)]);

    act(() => root.render(h(GapList, { gaps: [a], scope: "host" })));
    await flush();
    expect(visibleIds()).toEqual([]);
  });
});
