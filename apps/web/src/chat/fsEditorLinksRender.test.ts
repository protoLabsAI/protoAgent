// Render-level proof for the fs tools' "open in editor" links: mount the REAL ToolValue and
// assert what reaches the DOM. Roots are seeded straight into the console's QueryClient
// (fresh, so no fetch fires) and the pref is set through its own module. Every "no link"
// case must render exactly what the plain renderer does. (Mount pattern: waitRender.test.ts.)
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import { setEditorPref } from "../lib/editorPref";
import { queryClient } from "../lib/queryClient";
import { ToolValue } from "./tool-renderers";
import { FS_ROOTS_QUERY_KEY } from "./useEditorLinker";
import { parseFsArgs } from "./fsToolRenderers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(props: { raw: string; tool: string; input?: string }): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(ToolValue, { ...props, role: "output" }));
  });
  return host;
}

const ROOT = "/Users/me/My Repo";
const seedRoots = (roots: Record<string, string>) =>
  queryClient.setQueryData(FS_ROOTS_QUERY_KEY, { roots });

beforeEach(() => {
  queryClient.clear();
  seedRoots({ app: ROOT });
  setEditorPref("zed");
});

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  queryClient.clear();
});

const hrefs = (el: HTMLElement) => [...el.querySelectorAll("a.tool-editor-link")].map((a) => a.getAttribute("href"));

describe("read_file", () => {
  it("adds a header link to the file at the read's offset, content below unchanged", async () => {
    const el = await render({
      tool: "read_file",
      raw: "line 20\nline 21\n… (showing lines 20-21 of 90; call again with offset=22 for more)",
      input: '{"project": "app", "path": "src/main.py", "offset": 20}',
    });
    expect(hrefs(el)).toEqual(["zed://file/Users/me/My%20Repo/src/main.py:20"]);
    expect(el.querySelector(".tool-fs-head")?.textContent).toBe("src/main.py:20");
    expect(el.querySelector(".tool-text")?.textContent).toContain("line 20\nline 21");
    // Same-window hand-off — a _blank would open an empty tab before the OS prompt.
    expect(el.querySelector("a.tool-editor-link")?.getAttribute("target")).toBeNull();
  });

  it("links without a line when reading from the top", async () => {
    const el = await render({ tool: "read_file", raw: "hello", input: '{"project": "app", "path": "README.md"}' });
    expect(hrefs(el)).toEqual(["zed://file/Users/me/My%20Repo/README.md"]);
  });

  it("keeps the structured JSON view of a .json file under the header", async () => {
    const el = await render({
      tool: "read_file",
      raw: '{"name": "pkg"}',
      input: '{"project": "app", "path": "package.json"}',
    });
    expect(hrefs(el)).toHaveLength(1);
    expect(el.querySelector(".tool-kv")).toBeTruthy();
  });

  it("still links when the args preview was truncated mid-content", async () => {
    const el = await render({
      tool: "write_file",
      raw: "Created notes/todo.md (5000 chars).",
      input: '{"project": "app", "path": "notes/todo.md", "content": "a very long body that got cu',
    });
    expect(hrefs(el)).toEqual(["zed://file/Users/me/My%20Repo/notes/todo.md"]);
    expect(el.textContent).toBe("Created notes/todo.md (5000 chars).");
  });
});

describe("search_files", () => {
  const raw = [
    "src/a.py-9- def outer():",
    "src/a.py:10: x = call(1)",
    "--",
    "src/b #2.py:3: y = {1: 2}",
    "… (more matches or output; narrow the search or lower context_lines)",
  ].join("\n");

  it("links every file:line hit; context lines, separators and the cap note stay plain", async () => {
    const el = await render({ tool: "search_files", raw, input: '{"project": "app", "query": "x"}' });
    expect(hrefs(el)).toEqual([
      "zed://file/Users/me/My%20Repo/src/a.py:10",
      "zed://file/Users/me/My%20Repo/src/b%20%232.py:3",
    ]);
    expect([...el.querySelectorAll("a.tool-editor-link")].map((a) => a.textContent)).toEqual([
      "src/a.py:10",
      "src/b #2.py:3",
    ]);
    // The text is preserved verbatim — selection/copy reads the same as before.
    expect(el.querySelector(".tool-text")?.textContent).toBe(raw);
  });

  it("doesn't mistake a context line containing `:N: ` for a hit", async () => {
    const el = await render({
      tool: "search_files",
      raw: "src/a.py-4- d = {1:2: 3}\nsrc/a.py:5: hit",
      input: '{"project": "app", "query": "hit"}',
    });
    expect(hrefs(el)).toEqual(["zed://file/Users/me/My%20Repo/src/a.py:5"]);
  });

  it("(no matches) renders plain", async () => {
    const el = await render({ tool: "search_files", raw: "(no matches)", input: '{"project": "app", "query": "q"}' });
    expect(hrefs(el)).toEqual([]);
    expect(el.textContent).toBe("(no matches)");
  });
});

describe("find_files / edit_file", () => {
  it("links each found path, not the overflow note", async () => {
    const el = await render({
      tool: "find_files",
      raw: "src/a.py\nsrc/日本.py\n… (+3 more)",
      input: '{"project": "app", "pattern": "**/*.py"}',
    });
    expect(hrefs(el)).toEqual([
      "zed://file/Users/me/My%20Repo/src/a.py",
      "zed://file/Users/me/My%20Repo/src/%E6%97%A5%E6%9C%AC.py",
    ]);
    expect(el.textContent).toBe("src/a.py\nsrc/日本.py\n… (+3 more)");
  });

  it("links the edited path inside the confirmation", async () => {
    const el = await render({
      tool: "edit_file",
      raw: "Edited src/x.ts.",
      input: '{"project": "app", "path": "src/x.ts", "old": "a", "new": "b"}',
    });
    expect(hrefs(el)).toEqual(["zed://file/Users/me/My%20Repo/src/x.ts"]);
    expect(el.textContent).toBe("Edited src/x.ts.");
  });

  it("honors the chosen editor", async () => {
    setEditorPref("vscode");
    const el = await render({ tool: "edit_file", raw: "Edited src/x.ts.", input: '{"project": "app", "path": "src/x.ts"}' });
    expect(hrefs(el)).toEqual(["vscode://file/Users/me/My%20Repo/src/x.ts"]);
  });
});

describe("no link → exactly today's plain render", () => {
  const cases: Array<[string, () => void]> = [
    ["pref Off", () => setEditorPref("off")],
    ["unknown project", () => seedRoots({ other: "/x" })],
    ["roots not loaded", () => queryClient.clear()],
  ];
  for (const [name, setup] of cases) {
    it(name, async () => {
      setup();
      const el = await render({
        tool: "search_files",
        raw: "src/a.py:10: x = 1",
        input: '{"project": "app", "query": "x"}',
      });
      expect(hrefs(el)).toEqual([]);
      expect(el.querySelector(".tool-text")?.textContent).toBe("src/a.py:10: x = 1");
    });
  }

  it("an unreadable args preview (no project) renders plain", async () => {
    const el = await render({ tool: "read_file", raw: "body", input: '{"pa' });
    expect(hrefs(el)).toEqual([]);
    expect(el.textContent).toBe("body");
  });

  it("errors stay on the error renderer", async () => {
    const el = await render({
      tool: "read_file",
      raw: "Error: no such file: nope.py",
      input: '{"project": "app", "path": "nope.py"}',
    });
    expect(hrefs(el)).toEqual([]);
    expect(el.querySelector(".tool-error")).toBeTruthy();
  });

  // #3596 review: a failed find/write returns "Error: …" — never a path to link.
  for (const [tool, input] of [
    ["find_files", '{"project": "app", "pattern": "[bad"}'],
    ["write_file", '{"project": "app", "path": "src/x.ts", "content": "y"}'],
  ] as const) {
    it(`a ${tool} error is not linked`, async () => {
      const el = await render({ tool, raw: "Error: bad pattern: [bad", input });
      expect(hrefs(el)).toEqual([]);
      expect(el.querySelector(".tool-error")).toBeTruthy();
    });
  }
});

describe("parseFsArgs", () => {
  it("reads full JSON", () => {
    expect(parseFsArgs('{"project":"p","path":"a/b.py","offset":5}')).toEqual({ project: "p", path: "a/b.py", offset: 5 });
  });
  it("scrapes a truncated preview, unescaping strings", () => {
    expect(parseFsArgs('{"project": "p", "path": "a \\"q\\".py", "offset": 7, "content": "xx')).toEqual({
      project: "p",
      path: 'a "q".py',
      offset: 7,
    });
  });
  it("is empty for nothing usable", () => {
    expect(parseFsArgs(undefined)).toEqual({});
    expect(parseFsArgs("[1,2]")).toEqual({});
  });
});
