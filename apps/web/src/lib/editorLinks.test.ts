import { describe, expect, it } from "vitest";

import { editorUrl, joinProjectPath, makeEditorLinker } from "./editorLinks";

describe("editorUrl", () => {
  it("builds each editor's scheme with line and column", () => {
    expect(editorUrl("zed", "/Users/me/app/src/main.ts", 42, 7)).toBe("zed://file/Users/me/app/src/main.ts:42:7");
    expect(editorUrl("vscode", "/Users/me/app/src/main.ts", 42, 7)).toBe(
      "vscode://file/Users/me/app/src/main.ts:42:7",
    );
    expect(editorUrl("cursor", "/Users/me/app/src/main.ts", 42)).toBe("cursor://file/Users/me/app/src/main.ts:42");
  });

  it("omits the position when there is no valid line, and a column without a line", () => {
    expect(editorUrl("zed", "/a/b.ts")).toBe("zed://file/a/b.ts");
    expect(editorUrl("zed", "/a/b.ts", undefined, 3)).toBe("zed://file/a/b.ts");
    expect(editorUrl("zed", "/a/b.ts", 0)).toBe("zed://file/a/b.ts");
    expect(editorUrl("zed", "/a/b.ts", 1.5)).toBe("zed://file/a/b.ts");
  });

  it("returns null when Off or for an empty path", () => {
    expect(editorUrl("off", "/a/b.ts", 3)).toBeNull();
    expect(editorUrl("zed", "")).toBeNull();
  });

  it("percent-encodes spaces, # and ? so the path can't be cut at a fragment or query", () => {
    expect(editorUrl("zed", "/Users/me/My Project/notes #1?.md", 2)).toBe(
      "zed://file/Users/me/My%20Project/notes%20%231%3F.md:2",
    );
    expect(editorUrl("vscode", "/tmp/100%.txt")).toBe("vscode://file/tmp/100%25.txt");
  });

  it("encodes unicode as UTF-8 escapes (the editors URL-decode before opening)", () => {
    const url = editorUrl("zed", "/home/josé/日本/ß.rs", 1);
    expect(url).toBe("zed://file/home/jos%C3%A9/%E6%97%A5%E6%9C%AC/%C3%9F.rs:1");
    expect(decodeURIComponent(url!.slice("zed://file".length))).toBe("/home/josé/日本/ß.rs:1");
  });

  it("keeps a Windows drive literal and normalizes backslashes", () => {
    expect(editorUrl("vscode", "C:\\Users\\me\\proj\\a b.ts", 10)).toBe("vscode://file/C:/Users/me/proj/a%20b.ts:10");
    expect(editorUrl("zed", "D:/work/x.py")).toBe("zed://file/D:/work/x.py");
  });
});

describe("joinProjectPath", () => {
  it("joins a project-relative posix path onto the root", () => {
    expect(joinProjectPath("/srv/app", "src/x.ts")).toBe("/srv/app/src/x.ts");
    expect(joinProjectPath("/srv/app/", "./src/x.ts")).toBe("/srv/app/src/x.ts");
    expect(joinProjectPath("/srv/app", ".")).toBe("/srv/app");
  });

  it("joins onto a Windows root with forward slashes", () => {
    expect(joinProjectPath("C:\\proj", "src/x.ts")).toBe("C:/proj/src/x.ts");
    expect(joinProjectPath("C:\\", ".")).toBe("C:/");
  });

  it("refuses paths the fence would refuse — absolute, home, drive, escapes", () => {
    expect(joinProjectPath("/srv/app", "/etc/passwd")).toBeNull();
    expect(joinProjectPath("/srv/app", "~/x")).toBeNull();
    expect(joinProjectPath("/srv/app", "C:/x")).toBeNull();
    expect(joinProjectPath("/srv/app", "src/../../etc")).toBeNull();
    expect(joinProjectPath("", "x")).toBeNull();
  });
});

describe("makeEditorLinker", () => {
  const roots = { app: "/srv/app" };

  it("links a known project at a line", () => {
    const link = makeEditorLinker("zed", roots)!;
    expect(link("app", "src/x.ts", 12)).toBe("zed://file/srv/app/src/x.ts:12");
  });

  it("returns null for an unknown project (incl. prototype keys)", () => {
    const link = makeEditorLinker("zed", roots)!;
    expect(link("nope", "x.ts")).toBeNull();
    expect(link("constructor", "x.ts")).toBeNull();
  });

  it("is null when Off or roots aren't loaded", () => {
    expect(makeEditorLinker("off", roots)).toBeNull();
    expect(makeEditorLinker("zed", undefined)).toBeNull();
    expect(makeEditorLinker("zed", null)).toBeNull();
  });
});
