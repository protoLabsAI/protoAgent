import { describe, expect, it, vi } from "vitest";
import type { MouseEvent as ReactMouseEvent } from "react";

import { makeEditorLinker } from "../lib/editorLinks";
import { makeFileLinker } from "./useEditorLinker";

// Click routing for file links (ADR 0112): a plain click → the code pane, ⌘/Ctrl-click → the
// external editor, and the editor mode (#3596) flips it.

const ext = makeEditorLinker("zed", { app: "/r/app" });
const ev = (mod = false) =>
  ({ metaKey: mod, ctrlKey: false, preventDefault: vi.fn() }) as unknown as ReactMouseEvent<HTMLAnchorElement>;

describe("makeFileLinker — protoAgent mode", () => {
  it("plain click opens the pane with the range", () => {
    const open = vi.fn();
    const nav = vi.fn();
    const link = makeFileLinker("protoagent", "zed", ext, open, nav)!("app", "src/a.ts", 4, 9)!;
    expect(link.href).toBe("zed://file/r/app/src/a.ts:4");
    const e = ev();
    link.onClick!(e);
    expect(e.preventDefault).toHaveBeenCalled();
    expect(open).toHaveBeenCalledWith({ project: "app", path: "src/a.ts", line: 4, endLine: 9, source: "link" });
    expect(nav).not.toHaveBeenCalled();
  });

  it("⌘-click hands off to the external editor (same window)", () => {
    const open = vi.fn();
    const nav = vi.fn();
    const link = makeFileLinker("protoagent", "vscode", makeEditorLinker("vscode", { app: "/r/app" }), open, nav)!(
      "app",
      "a.ts",
      2,
    )!;
    link.onClick!(ev(true));
    expect(nav).toHaveBeenCalledWith("vscode://file/r/app/a.ts:2");
    expect(open).not.toHaveBeenCalled();
  });

  it("works with no roots / no external editor — ⌘-click then just opens the pane", () => {
    const open = vi.fn();
    const nav = vi.fn();
    const link = makeFileLinker("protoagent", "off", null, open, nav)!("remote-proj", "a.ts")!;
    expect(link.href).toBe("#");
    link.onClick!(ev(true));
    expect(open).toHaveBeenCalled();
    expect(nav).not.toHaveBeenCalled();
  });

  it("refuses paths outside the fence", () => {
    const linker = makeFileLinker("protoagent", "zed", ext, vi.fn())!;
    expect(linker("app", "../x.ts")).toBeNull();
    expect(linker("app", "/etc/passwd")).toBeNull();
    expect(linker("app", "~/x")).toBeNull();
  });
});

describe("makeFileLinker — editor mode", () => {
  it("is null without an external linker (pref Off, roots missing)", () => {
    expect(makeFileLinker("editor", "zed", null, vi.fn())).toBeNull();
  });

  it("the plain click follows the editor href; ⌘-click opens the pane instead", () => {
    const open = vi.fn();
    const link = makeFileLinker("editor", "zed", ext, open)!("app", "a.ts", 3)!;
    expect(link.href).toBe("zed://file/r/app/a.ts:3");
    const plain = ev();
    link.onClick!(plain);
    expect(plain.preventDefault).not.toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
    const mod = ev(true);
    link.onClick!(mod);
    expect(mod.preventDefault).toHaveBeenCalled();
    expect(open).toHaveBeenCalledWith({ project: "app", path: "a.ts", line: 3, endLine: undefined, source: "link" });
  });
});

describe("makeFileLinker — code pane toolset OFF (open = null)", () => {
  it("every link is the plain editor link, whatever the stored choice", () => {
    for (const mode of ["protoagent", "editor"] as const) {
      const link = makeFileLinker(mode, "zed", ext, null)!("app", "a.ts", 3)!;
      expect(link).toEqual({ href: "zed://file/r/app/a.ts:3", title: "Open in Zed" });
    }
  });

  it("is null without an external linker — there is no pane to fall back on", () => {
    expect(makeFileLinker("protoagent", "zed", null, null)).toBeNull();
  });
});
