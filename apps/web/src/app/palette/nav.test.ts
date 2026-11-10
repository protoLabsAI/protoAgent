import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useUI } from "../../state/uiStore";
import { applyNavIntent, navigate, openView, setPaletteNavigator } from "./nav";
import type { NavIntent } from "./nav";
import { registeredPaletteCommands } from "../../ext/paletteRegistry";
import "./registry";

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
    surfaceModal: null,
    surfaceStates: {},
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

describe("applyNavIntent — surface: opens the large shell MODAL on a section (#4955 §3.6)", () => {
  it.each([
    ["work", "Open Work", "overview"],
    ["work:decisions", "Work: Decisions", "decisions"],
    ["work:goals", "Work: Goals", "goals"],
    ["work:watches", "Work: Watches", "watches"],
    ["work:tasks", "Work: Tasks", "tasks"],
    ["work:schedules", "Work: Schedules", "schedules"],
    ["work:background", "Work: Background agents", "background"],
    ["work:activity", "Work: Activity", "activity"],
    ["work:directives", "Work: Directives", "directives"],
    ["work:new-goal", "Work: New goal…", "goals"],
  ])("registered row %s forwards and replays its modal destination", (id, label, section) => {
    const rows = registeredPaletteCommands("static").filter((row) => row.id === id);
    expect(rows).toHaveLength(1);
    expect(rows[0].label).toBe(label);
    const sink = vi.fn();
    const close = vi.fn();
    setPaletteNavigator(sink);
    rows[0].run({ close });
    const state = id === "work:new-goal" ? { section, filters: { create: true } } : { section };
    expect(sink).toHaveBeenCalledExactlyOnceWith({ kind: "surface", id: "work", state });
    expect(close).toHaveBeenCalledOnce();
    expect(useUI.getState().surfaceModal).toBeNull();
    applyNavIntent(JSON.parse(JSON.stringify(sink.mock.calls[0][0])));
    expect(useUI.getState().surfaceModal).toEqual({ viewId: "work", state });
  });

  // Every Work palette row (`registry.ts`) opens the modal through this arm. The eight
  // section ids are WorkView's own (`WorkOverview.workSections`).
  const SECTIONS = [
    "decisions", "goals", "watches", "tasks", "schedules", "background", "activity", "directives",
  ];

  it.each(["overview", ...SECTIONS, "new-goal"])("routes %s from another mobile surface", (section) => {
    useUI.setState({ mobileActive: "knowledge" });
    const state = section === "new-goal" ? { section: "goals", filters: { create: true } } : { section };
    applyNavIntent(JSON.parse(JSON.stringify({ kind: "surface", id: "work", state })) as NavIntent);
    expect(useUI.getState().mobileActive).toBe("knowledge");
    expect(useUI.getState().surfaceStates.work).toEqual(state);
    expect(useUI.getState().surfaceModal).toEqual({ viewId: "work", state });
    expect(useUI.getState().rightCollapsed).toBe(true);
  });

  it("opens the modal on the requested section and persists it as the saved place", () => {
    applyNavIntent({ kind: "surface", id: "work", state: { section: "decisions" } });
    const s = useUI.getState();
    expect(s.surfaceModal).toEqual({ viewId: "work", state: { section: "decisions" } });
    // openSurfaceModal writes the modal AND the view's saved place, so the modal remembers it.
    expect(s.surfaceStates.work).toEqual({ section: "decisions" });
  });

  it("Open Work lands on Overview", () => {
    applyNavIntent({ kind: "surface", id: "work", state: { section: "overview" } });
    expect(useUI.getState().surfaceModal).toEqual({ viewId: "work", state: { section: "overview" } });
  });

  it("New goal… lands on Goals with the create flag set in filters (Q15)", () => {
    applyNavIntent({ kind: "surface", id: "work", state: { section: "goals", filters: { create: true } } });
    const modal = useUI.getState().surfaceModal!;
    expect(modal.state.section).toBe("goals");
    expect(modal.state.filters).toEqual({ create: true });
  });

  it("without a state, opens the modal on the view's last-saved place", () => {
    useUI.setState({ surfaceStates: { work: { section: "tasks" } } });
    applyNavIntent({ kind: "surface", id: "work" });
    expect(useUI.getState().surfaceModal).toEqual({ viewId: "work", state: { section: "tasks" } });
  });

  it("every Work section row round-trips through the launcher's JSON Tauri payload", () => {
    for (const section of SECTIONS) {
      const intent: NavIntent = { kind: "surface", id: "work", state: { section } };
      // The launcher forwards the intent over a Tauri event — a JSON serialize/parse. Applying
      // the replayed copy must reproduce the in-process result, i.e. the arm is serializable.
      applyNavIntent(JSON.parse(JSON.stringify(intent)) as NavIntent);
      expect(useUI.getState().surfaceModal).toEqual({ viewId: "work", state: { section } });
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

  it("forwards a surface intent verbatim rather than opening the modal in the launcher window", () => {
    const sink = vi.fn();
    setPaletteNavigator(sink);
    const intent: NavIntent = { kind: "surface", id: "work", state: { section: "goals", filters: { create: true } } };
    navigate(intent);
    expect(sink).toHaveBeenCalledWith(intent);
    // The shell-less launcher window must not touch its own store — it hands the intent off.
    expect(useUI.getState().surfaceModal).toBeNull();
  });
});

describe("openView — a dock stored at 0 px opens at a usable size (#4076)", () => {
  it("right dock collapsed at width 0 reopens at the default width", () => {
    useUI.setState({ rightWidth: 0, rightCollapsed: true });
    openView("work");
    const s = useUI.getState();
    expect(s.rightCollapsed).toBe(false);
    expect(s.rightWidth).toBe(360);
  });

  it("a right width below the DS minimum is restored; a sane one is left alone", () => {
    useUI.setState({ rightWidth: 120, rightCollapsed: true });
    openView("work");
    expect(useUI.getState().rightWidth).toBe(360);
    useUI.setState({ rightWidth: 540, rightCollapsed: true });
    openView("work");
    expect(useUI.getState().rightWidth).toBe(540); // the operator's own width wins
  });

  it("bottom dock collapsed at height 0 reopens at the default height", () => {
    useUI.setState({ bottomHeight: 0, bottomCollapsed: true });
    openView("logs");
    expect(useUI.getState().bottomHeight).toBe(240);
    useUI.setState({ bottomHeight: 300, bottomCollapsed: true });
    openView("logs");
    expect(useUI.getState().bottomHeight).toBe(300);
  });
});
