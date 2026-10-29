// "Discover plugins" utility-bar pill (#4570) — the first visible, labelled way into the
// plugin marketplace. The pill itself is a one-liner inline in App.tsx's utility bar, wired
// `onClick={() => openPluginsDiscover()}`; App is not mountable in a unit test (boot gate,
// providers, the whole shell), so this renders the SAME DS Button with the SAME data-testid
// and the real store action, then asserts a real DOM click lands Settings on Plugins ▸ Discover.
// The store action is the contract both the pill and this test exercise (openPluginsDiscover),
// so this is where "click → plugins/discover selection" is pinned.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Button } from "@protolabsai/ui/primitives";
import { Puzzle } from "lucide-react";

import { useUI } from "../state/uiStore";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  // A clean slate: Settings closed, on the default section, with the default Installed tab —
  // so the click has somewhere to move FROM (the pill must flip Installed → Discover, not
  // just open on whatever tab was last used).
  useUI.setState({
    globalSettingsOpen: false,
    globalSettingsSection: undefined,
    settingsSection: "identity",
    pluginsTab: "local",
  });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

// The pill exactly as App.tsx wires it (DS Button + the real store action + the test id).
function Pill() {
  return (
    <Button
      size="xs"
      variant="ghost"
      type="button"
      aria-label="Discover plugins"
      data-testid="discover-plugins"
      onClick={() => useUI.getState().openPluginsDiscover()}
    >
      <Puzzle size={14} /> Discover plugins
    </Button>
  );
}

describe("Discover plugins pill", () => {
  it("renders a labelled, testid'd control", () => {
    act(() => root.render(<Pill />));
    const btn = container.querySelector<HTMLButtonElement>('[data-testid="discover-plugins"]');
    expect(btn).not.toBeNull();
    expect(btn!.getAttribute("aria-label")).toBe("Discover plugins");
    expect(btn!.textContent).toContain("Discover plugins");
  });

  it("opens Settings on Plugins ▸ Discover on click", () => {
    act(() => root.render(<Pill />));
    const btn = container.querySelector<HTMLButtonElement>('[data-testid="discover-plugins"]')!;

    expect(useUI.getState().globalSettingsOpen).toBe(false);

    act(() => btn.click());

    const s = useUI.getState();
    expect(s.globalSettingsOpen).toBe(true);
    // Plugins section, Discover (market) tab — the deep-link destination.
    expect(s.globalSettingsSection).toBe("plugins");
    expect(s.settingsSection).toBe("plugins"); // the authoritative landing section
    expect(s.pluginsTab).toBe("market");
  });
});
