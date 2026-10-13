// The list form of a path setting (`type: path` + `multiple: true`) — the data plugin's
// "Data folders". Pins: the stored value is ONE "\n"-joined string (old configs, older
// cores and the plugin's own comma/newline parser keep working), comma-separated legacy
// values load as rows, Add opens Browse… for the new row, Remove drops a row, duplicates
// collapse on save, and each row's Browse… fills THAT row only.
import { act, createElement as h, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { joinPathList, PathListPicker, splitPathList } from "../PathListPicker";
import { api } from "../../lib/api";
import type { BrowseListing } from "../../lib/types";

vi.mock("../../lib/desktop", async (importActual) => ({
  ...(await importActual<typeof import("../../lib/desktop")>()),
  hasDesktopShell: vi.fn(() => false),
  pickPathNative: vi.fn(async () => undefined),
}));

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const listing = (path: string): BrowseListing => ({
  path,
  parent: "/Users",
  entries: [],
  roots: [{ label: "Home", path: "/Users/kj" }],
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
  vi.clearAllMocks();
});

// A controlled host, like SettingsCategory's dirty map: the picker's emitted value is
// fed straight back in as `value`, so the echo path is exercised for real.
let setExternal: (v: string) => void = () => {};
async function mount(initial: string, onChange = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Host() {
    const [value, setValue] = useState(initial);
    setExternal = setValue;
    return h(PathListPicker, {
      value,
      label: "Data folders",
      onChange: (v: string) => {
        onChange(v);
        setValue(v);
      },
    });
  }
  await act(async () => {
    root.render(h(QueryClientProvider, { client: qc }, h(Host)));
  });
  return onChange;
}

const inputs = () => [...container.querySelectorAll<HTMLInputElement>(".path-picker-input")];
const button = (name: string | RegExp) =>
  [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => {
    const n = b.getAttribute("aria-label") || b.textContent || "";
    return typeof name === "string" ? n === name : name.test(n);
  })!;

async function click(el: Element) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

async function typeInto(input: HTMLInputElement, text: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function settle(pred: () => boolean) {
  for (let i = 0; i < 50 && !pred(); i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
  }
}

describe("splitPathList / joinPathList", () => {
  it("splits newlines AND commas, trims, drops blanks", () => {
    expect(splitPathList("/a, /b\n\n /c ,")).toEqual(["/a", "/b", "/c"]);
    expect(splitPathList("")).toEqual([]);
    expect(splitPathList(undefined)).toEqual([]);
  });

  it("joins with \\n, de-duplicated (first wins), blanks dropped — and round-trips", () => {
    expect(joinPathList(["/a", " /b ", "", "/a", "/c"])).toBe("/a\n/b\n/c");
    const legacy = "/data/one, /data/two";
    expect(joinPathList(splitPathList(legacy))).toBe("/data/one\n/data/two");
    expect(splitPathList(joinPathList(splitPathList(legacy)))).toEqual(["/data/one", "/data/two"]);
  });
});

describe("PathListPicker", () => {
  it("loads a legacy comma value as one row per folder, each labelled", async () => {
    await mount("/data/one, /data/two");
    expect(inputs().map((i) => i.value)).toEqual(["/data/one", "/data/two"]);
    expect(inputs().map((i) => i.getAttribute("aria-label"))).toEqual(["Data folders 1", "Data folders 2"]);
    expect(button("Browse for folder 2")).toBeTruthy();
    expect(button(/^Remove folder 1/)).toBeTruthy();
  });

  it("editing a row saves the whole list as a \\n-joined string", async () => {
    const onChange = await mount("/data/one,/data/two");
    await typeInto(inputs()[1], "/data/three");
    expect(onChange).toHaveBeenLastCalledWith("/data/one\n/data/three");
  });

  it("Remove drops that row", async () => {
    const onChange = await mount("/a\n/b\n/c");
    await click(button(/^Remove folder 2/));
    expect(inputs().map((i) => i.value)).toEqual(["/a", "/c"]);
    expect(onChange).toHaveBeenLastCalledWith("/a\n/c");
  });

  it("Add folder appends a row and opens Browse… for it; picking fills only that row", async () => {
    const browseDir = vi.spyOn(api, "browseDir").mockResolvedValue(listing("/Users/kj/sales"));
    const onChange = await mount("/a");
    await click(button(/Add folder/));
    // The new (blank) row is on screen even though a blank never reaches the saved value…
    expect(inputs().map((i) => i.value)).toEqual(["/a", ""]);
    expect(onChange).not.toHaveBeenCalled();
    // …and Browse… opened for it straight away.
    await settle(() => !!button("Use this folder") && !button("Use this folder").disabled);
    expect(browseDir).toHaveBeenCalledWith({ path: "", files: false });
    await click(button("Use this folder"));
    expect(inputs().map((i) => i.value)).toEqual(["/a", "/Users/kj/sales"]);
    expect(onChange).toHaveBeenLastCalledWith("/a\n/Users/kj/sales");
  });

  it("each row's Browse… replaces only its own row", async () => {
    vi.spyOn(api, "browseDir").mockResolvedValue(listing("/srv/new"));
    const onChange = await mount("/a\n/b");
    await click(button("Browse for folder 1"));
    await settle(() => !!button("Use this folder") && !button("Use this folder").disabled);
    await click(button("Use this folder"));
    expect(inputs().map((i) => i.value)).toEqual(["/srv/new", "/b"]);
    expect(onChange).toHaveBeenLastCalledWith("/srv/new\n/b");
  });

  it("de-duplicates on save", async () => {
    const onChange = await mount("/a\n/b");
    await typeInto(inputs()[1], "/a");
    expect(onChange).toHaveBeenLastCalledWith("/a");
  });

  it("re-seeds from an external value change (Discard) but not from its own echo", async () => {
    vi.spyOn(api, "browseDir").mockResolvedValue(listing("/Users/kj"));
    await mount("/a");
    await click(button(/Add folder/));
    await act(async () => {}); // the blank row survives its own (unchanged) value
    expect(inputs().length).toBe(2);
    await act(async () => setExternal("/x,/y"));
    expect(inputs().map((i) => i.value)).toEqual(["/x", "/y"]);
  });
});
