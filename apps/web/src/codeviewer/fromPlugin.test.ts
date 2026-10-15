import { describe, expect, it, vi } from "vitest";

import { codeOpenNotice, parsePluginCodeOpen, routePluginCodeOpen, targetLabel, type RouteDeps } from "./fromPlugin";

// `protoagent:code:open` from a plugin view (the Artifact panel's code-linked diagrams, ADR 0038
// amendment): the payload is shape-checked, then routed to the code pane, the external editor,
// or — with neither — a copied path.

const MSG = { type: "protoagent:code:open", project: "demo", path: "src/agent.py", line: 12, end_line: 20, note: "entry" };

describe("parsePluginCodeOpen", () => {
  it("reads a well-formed target", () => {
    expect(parsePluginCodeOpen(MSG)).toEqual({ project: "demo", path: "src/agent.py", line: 12, endLine: 20, note: "entry" });
  });

  it("refuses paths the fs fence would refuse, and non-targets", () => {
    for (const path of ["/etc/passwd", "~/.ssh/id_rsa", "../outside.py", "a/../../b", "C:/x", ""]) {
      expect(parsePluginCodeOpen({ ...MSG, path }), path).toBeNull();
    }
    expect(parsePluginCodeOpen({ ...MSG, type: "protoArtifact:openCode" })).toBeNull();
    expect(parsePluginCodeOpen({ ...MSG, project: 3 })).toBeNull();
    expect(parsePluginCodeOpen({ ...MSG, project: "x".repeat(201) })).toBeNull();
    expect(parsePluginCodeOpen(null)).toBeNull();
    expect(parsePluginCodeOpen("protoagent:code:open")).toBeNull();
  });

  it("drops a bad range instead of guessing one", () => {
    expect(parsePluginCodeOpen({ ...MSG, line: 0 })).toMatchObject({ line: undefined, endLine: undefined });
    expect(parsePluginCodeOpen({ ...MSG, line: "12" })).toMatchObject({ line: undefined });
    expect(parsePluginCodeOpen({ ...MSG, end_line: 5 })).toMatchObject({ line: 12, endLine: undefined });
    expect(parsePluginCodeOpen({ ...MSG, note: "x".repeat(400) })!.note).toHaveLength(280);
  });

  it("takes open_in only as 'pane' or 'editor' — anything else routes by preference", () => {
    expect(parsePluginCodeOpen(MSG)).not.toHaveProperty("openIn");
    expect(parsePluginCodeOpen({ ...MSG, open_in: "pane" })!.openIn).toBe("pane");
    expect(parsePluginCodeOpen({ ...MSG, open_in: "editor" })!.openIn).toBe("editor");
    for (const open_in of ["Editor", "zed", "", 1, true, null, { pane: true }]) {
      expect(parsePluginCodeOpen({ ...MSG, open_in }), String(open_in)).not.toHaveProperty("openIn");
    }
  });

  it("labels a target as project/path:range", () => {
    expect(targetLabel(parsePluginCodeOpen(MSG)!)).toBe("demo/src/agent.py:12-20");
    expect(targetLabel(parsePluginCodeOpen({ ...MSG, end_line: 12 })!)).toBe("demo/src/agent.py:12");
  });
});

function deps(over: Partial<RouteDeps> = {}): RouteDeps {
  return {
    paneOn: true,
    openIn: "protoagent",
    editor: "zed",
    roots: vi.fn(async () => ({ demo: "/work/demo" })),
    hostEditor: false,
    openHost: vi.fn(async () => true),
    open: vi.fn(),
    navigate: vi.fn(),
    copy: vi.fn(async () => true),
    ...over,
  };
}

describe("routePluginCodeOpen", () => {
  const t = parsePluginCodeOpen(MSG)!;

  it("opens the code pane at the range, with the note, when the pane is on", async () => {
    const d = deps();
    expect(await routePluginCodeOpen(t, d)).toBe("pane");
    expect(d.open).toHaveBeenCalledWith({
      project: "demo", path: "src/agent.py", line: 12, endLine: 20, note: "entry", source: "link",
    });
    expect(d.navigate).not.toHaveBeenCalled();
  });

  it("falls back to the external editor when the pane toolset is off", async () => {
    const d = deps({ paneOn: false });
    expect(await routePluginCodeOpen(t, d)).toBe("editor");
    expect(d.navigate).toHaveBeenCalledWith("zed://file/work/demo/src/agent.py:12");
    expect(d.open).not.toHaveBeenCalled();
  });

  it("honours 'Open files in: editor', and falls back to the pane without an editor link", async () => {
    const d = deps({ openIn: "editor", editor: "vscode" });
    expect(await routePluginCodeOpen(t, d)).toBe("editor");
    expect(d.navigate).toHaveBeenCalledWith("vscode://file/work/demo/src/agent.py:12");
    const d2 = deps({ openIn: "editor", roots: vi.fn(async () => null) });
    expect(await routePluginCodeOpen(t, d2)).toBe("pane");
  });

  it("copies the path when there is neither a pane nor an editor", async () => {
    const d = deps({ paneOn: false, editor: "off" });
    expect(await routePluginCodeOpen(t, d)).toBe("copied");
    expect(d.copy).toHaveBeenCalledWith("demo/src/agent.py:12-20");
    const d2 = deps({ paneOn: false, roots: vi.fn(async () => { throw new Error("401"); }), copy: vi.fn(async () => false) });
    expect(await routePluginCodeOpen(t, d2)).toBe("none");
  });

  it("never builds an editor link for a project the fence doesn't know", async () => {
    const d = deps({ paneOn: false, roots: vi.fn(async () => ({ other: "/work/other" })) });
    expect(await routePluginCodeOpen(t, d)).toBe("copied");
    expect(d.navigate).not.toHaveBeenCalled();
  });
});

describe("routePluginCodeOpen — explicit open_in", () => {
  const pane = parsePluginCodeOpen({ ...MSG, open_in: "pane" })!;
  const editor = parsePluginCodeOpen({ ...MSG, open_in: "editor" })!;

  it("'pane' opens the pane even when the operator prefers the editor", async () => {
    const d = deps({ openIn: "editor" });
    expect(await routePluginCodeOpen(pane, d)).toBe("pane");
    expect(d.navigate).not.toHaveBeenCalled();
  });

  it("'pane' falls back to the editor, then to a copy, while the pane is off", async () => {
    const d = deps({ paneOn: false });
    expect(await routePluginCodeOpen(pane, d)).toBe("editor");
    expect(d.navigate).toHaveBeenCalledWith("zed://file/work/demo/src/agent.py:12");
    const d2 = deps({ paneOn: false, editor: "off" });
    expect(await routePluginCodeOpen(pane, d2)).toBe("copied");
    expect(d2.copy).toHaveBeenCalledWith("demo/src/agent.py:12-20");
  });

  it("'editor' opens the editor even when the operator prefers the pane", async () => {
    const d = deps({ openIn: "protoagent" });
    expect(await routePluginCodeOpen(editor, d)).toBe("editor");
    expect(d.navigate).toHaveBeenCalledWith("zed://file/work/demo/src/agent.py:12");
    expect(d.open).not.toHaveBeenCalled();
  });

  it("'editor' falls back to the pane without an editor link, then to a copy", async () => {
    const off = deps({ editor: "off" });
    expect(await routePluginCodeOpen(editor, off)).toBe("pane");
    expect(off.roots).not.toHaveBeenCalled();
    const noRoots = deps({ roots: vi.fn(async () => null) });
    expect(await routePluginCodeOpen(editor, noRoots)).toBe("pane");
    const nothing = deps({ paneOn: false, editor: "off" });
    expect(await routePluginCodeOpen(editor, nothing)).toBe("copied");
    const noClipboard = deps({ paneOn: false, editor: "off", copy: vi.fn(async () => false) });
    expect(await routePluginCodeOpen(editor, noClipboard)).toBe("none");
  });
});

describe("routePluginCodeOpen — the host editor (filesystem.editor_command)", () => {
  const editor = parsePluginCodeOpen({ ...MSG, open_in: "editor" })!;
  const pane = parsePluginCodeOpen({ ...MSG, open_in: "pane" })!;

  it("is tried after the console's editor link, before the pane", async () => {
    const d = deps({ editor: "off", hostEditor: true });
    expect(await routePluginCodeOpen(editor, d)).toBe("host-editor");
    expect(d.openHost).toHaveBeenCalledWith(editor);
    expect(d.open).not.toHaveBeenCalled();
  });

  it("never runs when the console already has an editor link", async () => {
    const d = deps({ hostEditor: true });
    expect(await routePluginCodeOpen(editor, d)).toBe("editor");
    expect(d.openHost).not.toHaveBeenCalled();
  });

  it("is skipped unless the status says one is configured", async () => {
    const d = deps({ editor: "off", hostEditor: false });
    expect(await routePluginCodeOpen(editor, d)).toBe("pane");
    expect(d.openHost).not.toHaveBeenCalled();
  });

  it("falls through to the pane, then a copy, when the server refuses or throws", async () => {
    const refused = deps({ editor: "off", hostEditor: true, openHost: vi.fn(async () => false) });
    expect(await routePluginCodeOpen(editor, refused)).toBe("pane");
    const threw = deps({ paneOn: false, editor: "off", hostEditor: true, openHost: vi.fn(async () => { throw new Error("403"); }) });
    expect(await routePluginCodeOpen(editor, threw)).toBe("copied");
  });

  it("backs a 'pane' request while the pane is off", async () => {
    const d = deps({ paneOn: false, editor: "off", hostEditor: true });
    expect(await routePluginCodeOpen(pane, d)).toBe("host-editor");
  });
});

describe("codeOpenNotice", () => {
  const base = parsePluginCodeOpen(MSG)!;
  const pane = { ...base, openIn: "pane" as const };
  const editor = { ...base, openIn: "editor" as const };
  const on = { paneOn: true, editor: "zed" as const };
  const off = { paneOn: false, editor: "off" as const };

  it("keeps the pre-open_in toasts: silent unless copied or nowhere", () => {
    expect(codeOpenNotice(base, "pane", on)).toBeNull();
    expect(codeOpenNotice(base, "editor", on)).toBeNull();
    expect(codeOpenNotice(base, "copied", off)).toEqual({
      title: "Path copied", message: "demo/src/agent.py:12-20 — no code pane or editor is set up.",
    });
    expect(codeOpenNotice(base, "none", off)).toEqual({ title: "Code location", message: "demo/src/agent.py:12-20" });
  });

  it("is silent when the explicit destination was honoured", () => {
    expect(codeOpenNotice(pane, "pane", on)).toBeNull();
    expect(codeOpenNotice(editor, "editor", on)).toBeNull();
    expect(codeOpenNotice(editor, "host-editor", off)).toBeNull();
    expect(codeOpenNotice(base, "host-editor", off)).toBeNull();
  });

  it("'pane' landing elsewhere hints that the code pane toolset can be turned on", () => {
    const viaEditor = codeOpenNotice(pane, "editor", { paneOn: false, editor: "zed" })!;
    expect(viaEditor.title).toBe("Opened in Zed");
    expect(viaEditor.message).toContain("Tools ▸ Filesystem");
    const copied = codeOpenNotice(pane, "copied", off)!;
    expect(copied.title).toBe("Path copied");
    expect(copied.message).toContain("Tools ▸ Filesystem");
    const viaHost = codeOpenNotice(pane, "host-editor", off)!;
    expect(viaHost.title).toBe("Opened in your editor");
    expect(viaHost.message).toContain("Tools ▸ Filesystem");
  });

  it("'editor' landing elsewhere says why there was no editor", () => {
    expect(codeOpenNotice(editor, "pane", { paneOn: true, editor: "off" })!.message).toContain("Settings ▸ Chat ▸ Open files in");
    expect(codeOpenNotice(editor, "pane", on)!.message).toContain("No Zed link");
    const copied = codeOpenNotice(editor, "copied", off)!;
    expect(copied.title).toBe("Path copied");
    expect(copied.message).toMatch(/no external editor is set .* and the code pane is off\.$/);
  });
});
