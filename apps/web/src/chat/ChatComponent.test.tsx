// The built-in `table` renderer gains an optional attribution caption (ADR 0118 S1): a muted
// `Source: <text>` line under the table when `props.source` is a non-empty string. props are
// untrusted, so the renderer ignores a non-string source, caps it at 200 chars, and renders it
// as plain text (never HTML). A table without `source` must render exactly as it did before.
// These drive the real ChatComponent through createRoot/act, like the other console UI suites.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createFrameRegistry } from "../artifacts/inlineFrames";
import { registerChatComponent } from "../ext/componentRegistry";
import type { ComponentCatalogEntry } from "../lib/api/components";
import type { ComponentSpec } from "../lib/types";
import { ChatComponent } from "./ChatComponent";
import { ComponentFrameRegistryContext } from "./FrameComponentHost";

// The component catalog is the frame-resolution input (ADR 0118 D5 / S12b). Mock the hook so the
// resolution-order tests control it synchronously (and the built-in table tests run with an empty
// one — exactly the pre-S12 behavior). Mutated per test through this hoisted holder.
const catalogState = vi.hoisted(() => ({ rows: [] as ComponentCatalogEntry[] }));
vi.mock("../lib/api/components", () => ({
  useComponentCatalog: () => ({
    catalog: catalogState.rows,
    frameUrl: (name: string) => catalogState.rows.find((e) => e.name === name)?.frame_url ?? null,
  }),
}));

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const BASE_PROPS = { columns: ["City", "Pop"], rows: [["Austin", 961855]] };

let container: HTMLElement;
let root: Root;

function render(spec: ComponentSpec) {
  act(() => root.render(h(ChatComponent, { spec })));
}

function table(props: Record<string, unknown>): ComponentSpec {
  return { component: "table", props: { ...BASE_PROPS, ...props } };
}

function caption(): HTMLElement | null {
  return container.querySelector(".chat-comp-source");
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("ChatComponent table — source caption (ADR 0118 S1)", () => {
  it("renders a muted `Source: …` caption when source is a non-empty string", () => {
    render(table({ source: "Census Bureau, 2020" }));
    expect(caption()?.textContent).toBe("Source: Census Bureau, 2020");
    // The table itself still renders alongside the caption.
    expect(container.textContent).toContain("Austin");
  });

  it("renders no caption — and markup identical to before — when source is absent", () => {
    render(table({}));
    const without = container.querySelector(".chat-comp-table")!.innerHTML;
    expect(caption()).toBeNull();

    // Same props plus a source: the ONLY added node is the caption. Strip it and the markup
    // must match the no-source render byte-for-byte.
    render(table({ source: "somewhere" }));
    caption()!.remove();
    expect(container.querySelector(".chat-comp-table")!.innerHTML).toBe(without);
  });

  it("renders no caption for a non-string source", () => {
    for (const bad of [123, true, { url: "x" }, ["x"], null]) {
      render(table({ source: bad as unknown }));
      expect(caption()).toBeNull();
    }
  });

  it("renders no caption for an empty-string source", () => {
    render(table({ source: "" }));
    expect(caption()).toBeNull();
  });

  it("truncates a source longer than 200 chars to 200 chars", () => {
    const long = "x".repeat(500);
    render(table({ source: long }));
    const text = caption()!.textContent ?? "";
    expect(text).toBe("Source: " + "x".repeat(200));
    expect(text.replace("Source: ", "")).toHaveLength(200);
  });

  it("renders source as text, never HTML", () => {
    render(table({ source: "<b>injected</b>" }));
    // The angle brackets survive as literal text; no real <b> element is created.
    expect(caption()?.textContent).toBe("Source: <b>injected</b>");
    expect(caption()?.querySelector("b")).toBeNull();
  });
});

// Resolution order (ADR 0118 D5 / S12b): a TS-registered renderer wins, then a built-in, then a
// plugin FRAME from the catalog (rendered by FrameComponentHost in a sandboxed iframe), else a
// labelled "unsupported" note. The frame path is what lets a plugin ship a chat component with
// no console rebuild.
describe("ChatComponent — resolution order (ADR 0118 D5 / S12b)", () => {
  let unregister: (() => void) | null = null;

  afterEach(() => {
    unregister?.();
    unregister = null;
    catalogState.rows = [];
  });

  const frameRow: ComponentCatalogEntry = { name: "pl-demo", plugin: "demo", frame_url: "/plugins/demo/widget" };

  it("a TS-registered renderer wins over a frame kind of the same name (no iframe)", () => {
    catalogState.rows = [frameRow];
    unregister = registerChatComponent("pl-demo", () => h("div", { "data-testid": "ts-rendered" }, "TS wins"));
    render({ component: "pl-demo", props: {} });
    expect(container.querySelector('[data-testid="ts-rendered"]')?.textContent).toBe("TS wins");
    // The frame host must NOT render — the registered renderer claimed the kind.
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector('[data-testid="frame-component-host"]')).toBeNull();
  });

  it("a frame kind renders FrameComponentHost pointed at the catalog's frame_url, props forwarded", () => {
    catalogState.rows = [frameRow];
    render({ component: "pl-demo", props: { greeting: "hi" } });
    const host = container.querySelector('[data-testid="frame-component-host"]');
    expect(host).not.toBeNull();
    const frame = container.querySelector("iframe");
    expect(frame).not.toBeNull();
    expect(frame!.getAttribute("src")).toContain("/plugins/demo/widget");
    // The host is the bearer-free, scripts-only sandbox (no allow-same-origin) — same as S12a.
    expect(frame!.getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("an unknown kind with no frame shows the [unsupported component] note", () => {
    catalogState.rows = []; // nothing in the catalog
    render({ component: "mystery", props: {} });
    expect(container.querySelector(".chat-comp-unknown")?.textContent).toBe("[unsupported component: mystery]");
    expect(container.querySelector("iframe")).toBeNull();
  });

  // The fix behind S12b's rejected round: ChatComponent must hand every frame host the SHARED
  // per-chat-view registry (via ComponentFrameRegistryContext), NOT let each host mint its own
  // one-frame registry — otherwise the six-live-frame cap is never enforced and a transcript full
  // of plugin components keeps every iframe live. Seven distinct frame kinds under one registry
  // must settle at six live iframes + one evicted card. (jsdom has no IntersectionObserver, so the
  // hosts mount eagerly — same basis as the FrameComponentHost cap test.)
  it("shares one registry across frame components, enforcing the six-live-frame cap", () => {
    catalogState.rows = Array.from({ length: 7 }, (_, i) => ({
      name: `pl-frame-${i}`,
      plugin: "demo",
      frame_url: `/plugins/demo/w${i}`,
    }));
    const registry = createFrameRegistry();
    act(() =>
      root.render(
        h(
          ComponentFrameRegistryContext.Provider,
          { value: registry },
          ...catalogState.rows.map((r) => h(ChatComponent, { key: r.name, spec: { component: r.name, props: {} } })),
        ),
      ),
    );
    // One shared budget: six live frames, the seventh evicted to a static card.
    expect(registry.size()).toBe(6);
    expect(container.querySelectorAll("iframe").length).toBe(6);
    expect(container.querySelectorAll(".frame-component-host__evicted").length).toBe(1);
  });
});
