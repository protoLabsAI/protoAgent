import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useUI } from "../../state/uiStore";
import { applyNavIntent, navigate, openView, setPaletteNavigator } from "./nav";

// The dock-routing chokepoint (finding 14) had NO test, which is how it silently no-opped
// EVERY programmatic navigation on mobile until the `setMobileActive` fix: the mobile shell
// renders `mobileActive` and ignores the per-dock ids, so moving a dock the phone never
// draws looks exactly like nothing happening. These pin all four branches — left, right,
// bottom, and the hidden-surface restore — plus the two mobile invariants.

const RAIL = { left: ["chat", "knowledge", "memory"], right: ["work"], bottom: ["logs"], hidden: [] as string[] };

beforeEach(() => {
  useUI.setState({
    railOrder: { ...RAIL, hidden: [] },
    surface: "chat",
    rightPanel: "work",
    bottomPanel: "logs",
    leftCollapsed: true,
    rightCollapsed: true,
    bottomCollapsed: true,
    mobileActive: "chat",
  });
});

afterEach(() => {
  setPaletteNavigator(null);
  vi.restoreAllMocks();
});

describe("openView — routes to the dock the surface actually lives on", () => {
  it("left dock: sets the surface and uncollapses the left rail", () => {
    openView("knowledge");
    const s = useUI.getState();
    expect(s.surface).toBe("knowledge");
    expect(s.leftCollapsed).toBe(false);
  });

  it("right dock: sets the right panel and uncollapses it (not the left surface)", () => {
    openView("work");
    const s = useUI.getState();
    expect(s.rightPanel).toBe("work");
    expect(s.rightCollapsed).toBe(false);
    expect(s.surface).toBe("chat"); // untouched
  });

  it("bottom dock: sets the bottom panel and uncollapses it", () => {
    openView("logs");
    const s = useUI.getState();
    expect(s.bottomPanel).toBe("logs");
    expect(s.bottomCollapsed).toBe(false);
  });

  it("ALWAYS sets mobileActive — the mobile shell reads nothing else", () => {
    for (const id of ["knowledge", "work", "logs"]) {
      openView(id);
      expect(useUI.getState().mobileActive).toBe(id);
    }
  });

  it("un-hides a hidden surface first, then routes to the dock it was restored onto", () => {
    useUI.setState({ railOrder: { left: ["chat"], right: [], bottom: [], hidden: ["memory"] } });
    openView("memory");
    const s = useUI.getState();
    expect(s.railOrder.hidden).not.toContain("memory");
    // Restored onto a dock AND navigated to — the palette is the restore point, so a
    // hidden surface must not merely reappear, it must open.
    expect([s.surface, s.rightPanel, s.bottomPanel]).toContain("memory");
    expect(s.mobileActive).toBe("memory");
  });

  it("re-reads railOrder after the restore, so the route uses the dock it just landed on", () => {
    // "work" defaults to the RIGHT dock; hidden, it must still route right rather than
    // fall through to the left branch on a stale pre-restore snapshot.
    useUI.setState({ railOrder: { left: ["chat"], right: [], bottom: [], hidden: ["work"] } });
    openView("work");
    const s = useUI.getState();
    expect(s.rightPanel).toBe("work");
    expect(s.rightCollapsed).toBe(false);
  });
});

describe("applyNavIntent — the serializable intent the launcher forwards", () => {
  it("view: routes through openView", () => {
    applyNavIntent({ kind: "view", id: "knowledge" });
    expect(useUI.getState().surface).toBe("knowledge");
  });

  it("plugins: opens the Plugins settings section on the requested inner tab", () => {
    applyNavIntent({ kind: "plugins", tab: "market" });
    const s = useUI.getState();
    expect(s.pluginsTab).toBe("market");
    expect(s.globalSettingsOpen).toBe(true);
    expect(s.globalSettingsSection).toBe("plugins");
  });

  it("global: opens the settings dialog, with and without a section", () => {
    applyNavIntent({ kind: "global", section: "telemetry" });
    expect(useUI.getState().globalSettingsSection).toBe("telemetry");
    applyNavIntent({ kind: "global" });
    expect(useUI.getState().globalSettingsOpen).toBe(true);
    expect(useUI.getState().globalSettingsSection).toBeUndefined();
  });

  it("agent: navigates the window to the agent's slug route (ADR 0042)", () => {
    const href = vi.fn();
    const original = Object.getOwnPropertyDescriptor(window, "location")!;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, set href(v: string) { href(v); } },
    });
    try {
      applyNavIntent({ kind: "agent", slug: "ava" });
      expect(href).toHaveBeenCalledWith(expect.stringContaining("agent/ava/"));
    } finally {
      Object.defineProperty(window, "location", original);
    }
  });
});

describe("setPaletteNavigator — the launcher's window handoff", () => {
  it("diverts every navigation to the sink, leaving THIS window's store alone", () => {
    const sink = vi.fn();
    setPaletteNavigator(sink);
    navigate({ kind: "view", id: "knowledge" });
    expect(sink).toHaveBeenCalledWith({ kind: "view", id: "knowledge" });
    // The launcher window has no shell — mutating its store instead of forwarding is the
    // bug this indirection exists to prevent.
    expect(useUI.getState().surface).toBe("chat");
  });

  it("restores the local apply on null", () => {
    setPaletteNavigator(vi.fn());
    setPaletteNavigator(null);
    navigate({ kind: "view", id: "memory" });
    expect(useUI.getState().surface).toBe("memory");
  });
});
