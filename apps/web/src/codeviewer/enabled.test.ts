// The code pane as an opt-in toolset (ADR 0112 amendment): what the console does while the
// connected agent reports `code_pane.enabled` false — the chip renders inert, the live stream
// hooks do nothing — and that turning it on lights the same mounted chip up in place.
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import { useUI } from "../state/uiStore";
import { CodeRefChip } from "./CodeRefChip";
import { codePaneEnabledFrom, isCodePaneEnabled, setCodePaneEnabled } from "./enabled";
import { onLiveComponent, onLiveToolEvent } from "./live";
import { resetFollowThrottle } from "./open";
import { resetCodeViewer, setFollow, useCodeViewer } from "./store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

const PROPS = { project: "app", path: "src/router.py", line: 4, end_line: 9, note: "the retry budget resets here" };

async function renderChip(): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(CodeRefChip, { props: PROPS }));
  });
  return host;
}

beforeEach(() => {
  resetCodeViewer();
  resetFollowThrottle();
  setCodePaneEnabled(false);
  useUI.setState({ railOrder: { left: ["chat"], right: ["work", "code"], bottom: [], hidden: [] } });
});

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  setCodePaneEnabled(false);
});

describe("codePaneEnabledFrom", () => {
  it("reads runtime status code_pane.enabled; absent (older server / no status) is off", () => {
    expect(codePaneEnabledFrom({ code_pane: { enabled: true } })).toBe(true);
    expect(codePaneEnabledFrom({ code_pane: { enabled: false } })).toBe(false);
    expect(codePaneEnabledFrom({})).toBe(false);
    expect(codePaneEnabledFrom(null)).toBe(false);
  });
  it("defaults to off", () => {
    expect(isCodePaneEnabled()).toBe(false);
  });
});

describe("code-ref chip", () => {
  it("OFF: a history chip renders as inert `project/path:lines — note` text, no button", async () => {
    const el = await renderChip();
    expect(el.querySelector("button")).toBeNull();
    expect(el.querySelector('[data-testid="code-ref-inert"]')?.textContent).toBe(
      "app/src/router.py:4-9 — the retry budget resets here",
    );
  });

  it("ON: the chip is a button that opens the pane; flipping the toolset re-renders in place", async () => {
    const el = await renderChip();
    await act(async () => setCodePaneEnabled(true));
    const chip = el.querySelector<HTMLButtonElement>('[data-testid="code-ref-chip"]');
    expect(chip).toBeTruthy();
    await act(async () => chip!.click());
    expect(useCodeViewer.getState().current).toMatchObject({ project: "app", path: "src/router.py", line: 4 });
    await act(async () => setCodePaneEnabled(false));
    expect(el.querySelector('[data-testid="code-ref-inert"]')).toBeTruthy();
  });
});

describe("live stream hooks", () => {
  const comp = { component: "code-ref", props: PROPS };
  const readEnd = {
    id: "t1",
    name: "read_file",
    phase: "end" as const,
    output: "line",
  };

  it("OFF: a live code-ref and a follow-mode fs call open nothing", () => {
    setFollow(true);
    onLiveComponent(comp);
    onLiveToolEvent(readEnd as never, '{"project": "app", "path": "src/a.py"}');
    expect(useCodeViewer.getState().current).toBeNull();
  });

  it("ON: a live code-ref opens the pane", () => {
    setCodePaneEnabled(true);
    onLiveComponent(comp);
    expect(useCodeViewer.getState().current).toMatchObject({ path: "src/router.py", source: "component" });
  });
});
