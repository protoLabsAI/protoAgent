// The built-in `table` renderer gains an optional attribution caption (ADR 0118 S1): a muted
// `Source: <text>` line under the table when `props.source` is a non-empty string. props are
// untrusted, so the renderer ignores a non-string source, caps it at 200 chars, and renders it
// as plain text (never HTML). A table without `source` must render exactly as it did before.
// These drive the real ChatComponent through createRoot/act, like the other console UI suites.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ComponentSpec } from "../lib/types";
import { ChatComponent } from "./ChatComponent";

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
