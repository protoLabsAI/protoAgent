// The folder picker behind every path-valued setting. Typing is the one input method
// that can't tell you the path doesn't exist, and a bad path is expensive — an unusable
// work folder is skipped at graph build, and if it was the only one the whole fs toolset
// unbinds. These pin the behaviors that make picking safe:
//   • folder rows NAVIGATE on one click (no select-vs-descend ambiguity, no nested click
//     target a keyboard can't reach); "Use this folder" confirms wherever you landed
//   • a seeded path that no longer resolves falls back to the server default instead of
//     dead-ending the dialog — that's exactly when someone reaches for Browse
//   • it browses the SERVER (api.browseDir), never the browser's own filesystem
//
// jsdom + react-dom/client (the console has no @testing-library; the unit harness is
// `.test.ts` only, so we build elements with React.createElement rather than JSX).
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PathPicker } from "../PathPicker";
import { api } from "../../lib/api";
import type { BrowseListing } from "../../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const listing = (over: Partial<BrowseListing> = {}): BrowseListing => ({
  path: "/Users/kj",
  parent: "/Users",
  entries: [
    { name: "dev", path: "/Users/kj/dev", kind: "dir" },
    { name: "Documents", path: "/Users/kj/Documents", kind: "dir" },
  ],
  roots: [{ label: "Home", path: "/Users/kj" }],
  ...over,
});

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

async function mount(props: { value?: string; kind?: "dir" | "file"; onChange?: (v: string) => void } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      h(QueryClientProvider, { client: qc }, h(PathPicker, {
        value: props.value ?? "",
        kind: props.kind,
        onChange: props.onChange ?? (() => {}),
      })),
    );
  });
}

/** Click Browse… and let react-query settle the first listing. */
async function openBrowser() {
  const browse = [...container.querySelectorAll("button")].find((b) => b.textContent?.includes("Browse"))!;
  await act(async () => {
    browse.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  for (let i = 0; i < 50 && !document.querySelector(".path-browser-row"); i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
  }
}

const rows = () => [...document.querySelectorAll<HTMLButtonElement>(".path-browser-row")];
const cwdText = () => document.querySelector(".path-browser-cwd")?.textContent ?? "";
const confirmButton = () =>
  [...document.querySelectorAll("button")].find(
    (b) => b.textContent === "Use this folder" || b.textContent === "Select file",
  )!;

async function click(el: Element) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

describe("PathPicker", () => {
  it("browses the server, not the browser — and confirms the folder you navigated to", async () => {
    const browseDir = vi.spyOn(api, "browseDir").mockResolvedValue(listing());
    const onChange = vi.fn();
    await mount({ onChange });
    await openBrowser();

    expect(browseDir).toHaveBeenCalled();
    expect(rows().map((r) => r.textContent)).toEqual(["dev", "Documents"]);

    // Navigating into `dev` re-queries the server for THAT path…
    browseDir.mockResolvedValue(listing({ path: "/Users/kj/dev", parent: "/Users/kj", entries: [] }));
    await click(rows()[0]);
    // Wait for the NEW listing to land — the previous one is deliberately held on screen
    // while it loads, so "no rows yet" is not the settle signal; the path bar is.
    for (let i = 0; i < 50 && cwdText() !== "/Users/kj/dev"; i++) {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 10));
      });
    }
    expect(browseDir).toHaveBeenLastCalledWith({ path: "/Users/kj/dev", files: false });
    expect(document.querySelector(".path-browser-empty")?.textContent).toBe("This folder is empty.");

    // …and confirming takes where you ARE, so one click per level is the whole gesture.
    await click(confirmButton());
    expect(onChange).toHaveBeenCalledWith("/Users/kj/dev");
  });

  it("falls back to the server default when the seeded path is gone", async () => {
    // The field holds a stale/typo'd path — precisely when someone opens the picker. It
    // must not dead-end on the error; it drops to the default listing and says why.
    const browseDir = vi
      .spyOn(api, "browseDir")
      .mockRejectedValueOnce(new Error("no such folder: /gone"))
      .mockResolvedValue(listing());
    await mount({ value: "/gone" });
    await openBrowser();

    expect(browseDir).toHaveBeenNthCalledWith(1, { path: "/gone", files: false });
    expect(browseDir).toHaveBeenLastCalledWith({ path: "", files: false });
    expect(rows().length).toBe(2);
    expect(document.querySelector(".path-browser-note")?.textContent).toContain("doesn’t exist");
  });

  it("in file mode asks the server for files and only confirms a file", async () => {
    vi.spyOn(api, "browseDir").mockResolvedValue(
      listing({ entries: [
        { name: "dev", path: "/Users/kj/dev", kind: "dir" },
        { name: "chat.db", path: "/Users/kj/chat.db", kind: "file" },
      ] }),
    );
    const onChange = vi.fn();
    await mount({ kind: "file", onChange });
    await openBrowser();

    expect(api.browseDir).toHaveBeenCalledWith({ path: "", files: true });
    // Nothing picked yet → can't confirm (a folder is not a file).
    expect(confirmButton().hasAttribute("disabled")).toBe(true);

    await click(rows()[1]); // the file
    expect(rows()[1].getAttribute("aria-selected")).toBe("true");
    await click(confirmButton());
    expect(onChange).toHaveBeenCalledWith("/Users/kj/chat.db");
  });

  it("keeps the text input editable — pasting a known path stays the fast route", async () => {
    const onChange = vi.fn();
    await mount({ value: "/Users/kj", onChange });
    const input = container.querySelector<HTMLInputElement>(".path-picker-input")!;
    expect(input.value).toBe("/Users/kj");

    // React attaches its own onChange to the native input event.
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
    await act(async () => {
      setter.call(input, "/srv/data");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(onChange).toHaveBeenCalledWith("/srv/data");
  });
});
