// ManagedProjectsList is mounted for real inside a QueryClientProvider; only the api client is
// faked. The access switch, its confirmation, the pending row and the inline failure are all
// driven through the DOM, the way an operator uses them (projectBoard-plugin#783).

import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "@protolabsai/ui/overlays";
import type { FsProject, ManagedProject, ManagedProjects, ToolInfo } from "../lib/types";

const { managedProjects, setProjectWrite, tools, fsProjects, setFsProjects } = vi.hoisted(() => ({
  managedProjects: vi.fn(),
  setProjectWrite: vi.fn(),
  tools: vi.fn(),
  fsProjects: vi.fn(),
  setFsProjects: vi.fn(),
}));
vi.mock("../lib/api", () => ({
  api: { managedProjects, setProjectWrite, tools, fsProjects, setFsProjects },
  currentSlug: () => "", // the query keys are scoped by fleet member; "" is the host
}));
// The panel's neighbours have their own tests; here they'd only drag in more of the api client.
vi.mock("./PythonRuntimeCard", () => ({ PythonRuntimeCard: () => null }));
vi.mock("../settings/QuickSetting", () => ({ QuickSetting: () => null }));
vi.mock("../settings/PathPicker", () => ({
  PathPicker: ({ value }: { value: string }) => h("input", { "aria-label": "Folder path", value, readOnly: true }),
}));

import { FsProjectsEditor, ManagedProjectsList, ToolsPanel } from "./ToolsPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
let qc: QueryClient;

const PROJECTS: ManagedProject[] = [
  { name: "locked", path: "/p/locked", write: false, fenced: true, exists: true },
  { name: "open", path: "/p/open", write: true, fenced: true, exists: true },
  { name: "keeper", path: "/p/keeper", write: true, no_delete: true, fenced: true, exists: true },
  { name: "boardonly", path: "/p/boardonly", fs: false, fenced: false, exists: true },
  { name: "gone", path: "/p/gone", write: false, fenced: false, exists: false },
];

function registry(overrides: Partial<ManagedProjects> = {}): ManagedProjects {
  return { enabled: true, fence_source: "registry", projects: PROJECTS, ...overrides };
}

async function flush() {
  // react-query batches its notifications on a timer, so yield real macrotasks.
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mount(data: ManagedProjects = registry()) {
  managedProjects.mockResolvedValue(data);
  await mountNode(h(ManagedProjectsList));
}

async function mountNode(node: ReturnType<typeof h>) {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(h(QueryClientProvider, { client: qc }, h(ToastProvider, null, node))));
  await flush();
}

// Raw config keys and code names an operator should never have to read in a sentence.
const JARGON = ["fs: false", "read_file", "list_dir", "projects:", "unbound"];
function expectPlain(text: string) {
  for (const word of JARGON) expect(text).not.toContain(word);
}
function warning(): string {
  const found = Array.from(host!.querySelectorAll<HTMLElement>('.fs-projects-warning[role="alert"]'));
  expect(found).toHaveLength(1);
  return found[0].textContent ?? "";
}

function sw(name: string): HTMLInputElement {
  const el = host!.querySelector<HTMLInputElement>(`input[aria-label="Allow writes in ${name}"]`);
  if (!el) throw new Error(`no switch for ${name}`);
  return el;
}
function switches(): HTMLInputElement[] {
  return Array.from(host!.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));
}
async function click(el: HTMLElement) {
  await act(async () => el.click());
  await flush();
}
function dialogButton(text: string): HTMLButtonElement | undefined {
  return Array.from(document.body.querySelectorAll("button")).find((b) => b.textContent?.trim() === text);
}
function alerts(): HTMLElement[] {
  return Array.from(host!.querySelectorAll<HTMLElement>(".fs-projects-error"));
}

beforeEach(() => {
  managedProjects.mockReset();
  setProjectWrite.mockReset();
  tools.mockReset();
  fsProjects.mockReset();
  setFsProjects.mockReset();
});

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

describe("Managed projects ▸ access switch", () => {
  it("targets the selected folder when project names repeat", async () => {
    setProjectWrite.mockResolvedValue({ ok: true });
    await mount(registry({ projects: [
      { ...PROJECTS[1], name: "duplicate", path: "/first" },
      { ...PROJECTS[1], name: "duplicate", path: "/second" },
    ] }));
    await click(switches()[1]);
    expect(setProjectWrite).toHaveBeenCalledWith("duplicate", false, "/second");
  });

  it("shows a labelled switch reflecting write on every row except `fs: false`", async () => {
    await mount();
    expect(sw("locked").checked).toBe(false);
    expect(sw("open").checked).toBe(true);
    expect(sw("keeper").checked).toBe(true);
    expect(sw("gone").checked).toBe(false);
    expect(sw("gone").disabled).toBe(false);
    expect(switches()).toHaveLength(4);
    // The no-fs row keeps an empty cell so the badges stay in one column.
    const rows = Array.from(host!.querySelectorAll(".fs-projects-row"));
    const noFs = rows.find((r) => r.textContent?.includes("boardonly"))!;
    expect(noFs.querySelector("span.fs-projects-write")).not.toBeNull();
    expect(noFs.querySelector("input")).toBeNull();
    expect(noFs.textContent).toContain("no fs");
    const intro = host!.querySelector(".setup-hint")!.textContent ?? "";
    expect(intro).toBe("Projects from your config file. Switch each between read-only and read-write.");
    expectPlain(intro);
  });

  it("asks before raising to read-write and sends write:true only on confirm", async () => {
    setProjectWrite.mockResolvedValue({ ok: true, name: "locked", path: "/p/locked", write: true });
    await mount();
    await click(sw("locked"));
    expect(setProjectWrite).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("Let coders edit files in locked?");
    expect(document.body.textContent).toContain("create, change and delete files");

    await click(dialogButton("Allow writes")!);
    expect(setProjectWrite).toHaveBeenCalledTimes(1);
    expect(setProjectWrite).toHaveBeenCalledWith("locked", true, "/p/locked");
    expect(dialogButton("Allow writes")).toBeUndefined();
  });

  it("cancelling the confirmation sends nothing and leaves the switch off", async () => {
    await mount();
    await click(sw("locked"));
    await click(dialogButton("Cancel")!);
    expect(setProjectWrite).not.toHaveBeenCalled();
    expect(dialogButton("Allow writes")).toBeUndefined();
    expect(sw("locked").checked).toBe(false);
  });

  it("lowers to read-only at once, without a confirmation", async () => {
    setProjectWrite.mockResolvedValue({ ok: true, name: "open", path: "/p/open", write: false });
    await mount();
    await click(sw("open"));
    expect(dialogButton("Allow writes")).toBeUndefined();
    expect(setProjectWrite).toHaveBeenCalledWith("open", false, "/p/open");
  });

  it("a no-delete row sends write:false when turned off", async () => {
    setProjectWrite.mockResolvedValue({ ok: true, name: "keeper", path: "/p/keeper", write: false });
    await mount();
    await click(sw("keeper"));
    expect(setProjectWrite).toHaveBeenCalledWith("keeper", false, "/p/keeper");
  });

  it("is busy and says Saving… while the request runs, then refetches the registry", async () => {
    let resolve!: (v: unknown) => void;
    setProjectWrite.mockReturnValue(new Promise((r) => (resolve = r)));
    await mount();
    await click(sw("open"));

    expect(sw("open").disabled).toBe(true);
    expect(sw("open").getAttribute("aria-busy")).toBe("true");
    expect(sw("open").checked).toBe(true); // no optimistic flip
    expect(sw("open").closest("label.fs-projects-write")!.textContent).toBe("Saving…");
    expect(sw("locked").disabled).toBe(false); // pending is per row

    managedProjects.mockResolvedValue(
      registry({ projects: PROJECTS.map((p) => (p.name === "open" ? { ...p, write: false } : p)) }),
    );
    const before = managedProjects.mock.calls.length;
    await act(async () => resolve({ ok: true, name: "open", path: "/p/open", write: false }));
    await flush();

    expect(managedProjects.mock.calls.length).toBe(before + 1);
    expect(sw("open").checked).toBe(false);
    expect(sw("open").disabled).toBe(false);
    expect(sw("open").closest("label.fs-projects-write")!.textContent).toBe("write");
    expect(alerts()).toHaveLength(0);
  });

  it("keeps the previous state and shows an inline alert under the failed row only", async () => {
    setProjectWrite.mockRejectedValue(new Error("config is read-only"));
    await mount();
    await click(sw("open"));

    expect(sw("open").checked).toBe(true);
    expect(sw("open").disabled).toBe(false);
    const found = alerts();
    expect(found).toHaveLength(1);
    expect(found[0].getAttribute("role")).toBe("alert");
    expect(found[0].id).toBe("mp-err-1");
    expect(found[0].textContent).toBe("Couldn't change access: config is read-only");
    expect(sw("open").getAttribute("aria-describedby")).toBe("mp-err-1");
    expect(sw("locked").getAttribute("aria-describedby")).toBeNull();
    // The alert sits directly under the row it belongs to.
    expect(found[0].previousElementSibling!.textContent).toContain("open");

    // The next attempt clears it as soon as it starts.
    setProjectWrite.mockReturnValue(new Promise(() => {}));
    await click(sw("open"));
    expect(alerts()).toHaveLength(0);
  });

  it("disables every switch when explicit Work folders shadow the registry", async () => {
    await mount(registry({ fence_source: "explicit" }));
    expect(switches()).toHaveLength(4);
    for (const el of switches()) {
      expect(el.disabled).toBe(true);
      expect(el.title).toBe("Explicit Work folders override the registry");
    }
    await click(sw("open"));
    expect(setProjectWrite).not.toHaveBeenCalled();
  });
});

describe("Managed projects ▸ file-access warnings", () => {
  it("says no project allows file access, and what to do, when every project opts out", async () => {
    await mount(registry({
      fence_source: "unbound",
      projects: [
        { name: "a", path: "/p/a", fs: false, fenced: false, exists: true },
        // The registry can hand `fs` back as a string; "no" still means opted out.
        { name: "b", path: "/p/b", fs: "no" as unknown as boolean, fenced: false, exists: true },
      ],
    }));
    const text = warning();
    expect(text).toBe(
      "The agent can't open any files. No project allows file access. To allow it, turn file access on for a project in your config file.",
    );
    expectPlain(text);
  });

  it("says the folders are missing, and how to fix it, when none exist", async () => {
    await mount(registry({
      fence_source: "unbound",
      projects: [{ name: "gone", path: "/p/gone", write: false, fenced: false, exists: false }],
    }));
    const text = warning();
    expect(text).toBe(
      "The agent can't open any files. None of these projects' folders exist. Restore them, or fix the paths in your config file.",
    );
    expectPlain(text);
  });

  it("shows no file-access warning while the registry is fencing folders", async () => {
    await mount();
    expect(host!.querySelectorAll(".fs-projects-warning")).toHaveLength(0);
  });
});

describe("Work folders editor", () => {
  const saved = (rows: FsProject[]) => ({ enabled: true, projects: rows });

  it("warns in plain words when none of the saved folders exist", async () => {
    fsProjects.mockResolvedValue(saved([
      { path: "/gone/one", write: false, exists: false },
      { path: "/gone/two", write: true, exists: false },
    ]));
    await mountNode(h(FsProjectsEditor));
    const text = warning();
    expect(text).toBe(
      "The agent can't open any files. None of these folders exist. Restore them, or fix or remove the paths below.",
    );
    expectPlain(text);
  });

  it("confirms a save by saying what the agent can now do", async () => {
    fsProjects.mockResolvedValue(saved([{ path: "/work/app", write: false, exists: true }]));
    setFsProjects.mockImplementation(async (rows: FsProject[]) => saved(rows));
    await mountNode(h(FsProjectsEditor));
    expect(host!.querySelectorAll(".fs-projects-warning")).toHaveLength(0);

    // Removing the row makes the list dirty, which arms Save.
    await click(host!.querySelector<HTMLButtonElement>('button[aria-label="Remove folder"]')!);
    await click(Array.from(host!.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Save folders")!);

    expect(setFsProjects).toHaveBeenCalledWith([]);
    expect(document.body.querySelector(".pl-toast__msg")!.textContent).toBe("The agent can open files in these folders.");
    expect(document.body.textContent).not.toContain("Filesystem tools cover");
  });
});

describe("Tools panel ▸ header and rows", () => {
  const TOOLS: ToolInfo[] = [
    { name: "web_search", description: "Search the web.", source: "core", category: "General", enabled: true },
    { name: "bare_tool", description: "", source: "core", category: "General", enabled: false },
    { name: "kb_ingest", description: "Add a document.", source: "core", category: "General", enabled: true, subagent_only: true },
    { name: "read_file", description: "Read a file.", source: "core", category: "Filesystem", enabled: true },
    { name: "board_add", description: "Add a card.", source: "plugin", category: "Project Board", enabled: true },
    { name: "orphan", description: "No owner.", source: "plugin", category: "Plugin", enabled: true },
    { name: "echo__ping", description: "Echo ping.", source: "mcp", category: "echo", enabled: false },
  ];

  async function mountPanel() {
    // `count` is the lead's wired tools (no subagent-only row) — the header must not echo it.
    tools.mockResolvedValue({ tools: TOOLS, count: 4, disabled: ["bare_tool", "echo__ping"] });
    await mountNode(h(ToolsPanel));
  }
  function row(name: string): HTMLElement {
    const el = Array.from(host!.querySelectorAll<HTMLElement>(".tools-row")).find(
      (r) => r.querySelector(".tools-name")?.textContent === name,
    );
    if (!el) throw new Error(`no row for ${name}`);
    return el;
  }
  function groupHead(name: string): string {
    const el = Array.from(host!.querySelectorAll<HTMLElement>(".tools-group-head")).find((g) =>
      g.textContent?.startsWith(name),
    );
    if (!el) throw new Error(`no group ${name}`);
    return el.textContent ?? "";
  }

  it("heads the list with what the agent can do and the on / off counts", async () => {
    await mountPanel();
    const text = host!.textContent ?? "";
    expect(text).toContain("What the agent can do · 5 on · 2 off");
    expect(text).not.toContain("wired tool");
    expect(text).not.toContain("subagent-only");
    expect(text).not.toMatch(/\d+ groups?/);
  });

  it("leads each row with its description and puts the code name under it", async () => {
    await mountPanel();
    const main = row("web_search").querySelector(".tools-row-main")!;
    expect(Array.from(main.children).map((c) => [c.tagName, c.className, c.textContent])).toEqual([
      ["SPAN", "tools-desc", "Search the web."],
      ["CODE", "tools-name", "web_search"],
    ]);
    // A tool with no description still shows its name, and nothing empty above it.
    expect(row("bare_tool").querySelector(".tools-desc")).toBeNull();
    expect(row("bare_tool").querySelector(".tools-row-main")!.children).toHaveLength(1);
  });

  it("marks a subagent-only row in plain words, after the description and name", async () => {
    await mountPanel();
    const main = row("kb_ingest").querySelector(".tools-row-main")!;
    expect(Array.from(main.children).map((c) => c.textContent)).toEqual([
      "Add a document.",
      "kb_ingest",
      "Only subagents can use this",
    ]);
    expect(host!.textContent).not.toContain("Subagents only");
    expect(row("web_search").textContent).not.toContain("Only subagents can use this");
  });

  it("says where a plugin or MCP group came from, and drops the unlabelled settings icon", async () => {
    await mountPanel();
    expect(groupHead("Project Board")).toContain("From Project Board");
    expect(groupHead("echo")).toContain("From echo");
    // The backend's fallback category is a bare "Plugin" — never "From Plugin".
    expect(groupHead("Plugin")).toContain("From a plugin");
    expect(groupHead("General")).not.toContain("From");
    expect(groupHead("Filesystem")).not.toContain("From");
    expect(host!.querySelector('[aria-label="has settings"]')).toBeNull();
    expect(host!.querySelector(".tools-group-cog")).toBeNull();
  });
});
