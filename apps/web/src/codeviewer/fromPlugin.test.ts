import { describe, expect, it, vi } from "vitest";

import { parsePluginCodeOpen, routePluginCodeOpen, targetLabel, type RouteDeps } from "./fromPlugin";

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
