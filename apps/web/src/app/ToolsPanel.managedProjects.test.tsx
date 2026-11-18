// ManagedProjectsList is mounted for real inside a QueryClientProvider; only the api client is
// faked. The access switch, its confirmation, the pending row and the inline failure are all
// driven through the DOM, the way an operator uses them (projectBoard-plugin#783).

import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ManagedProject, ManagedProjects } from "../lib/types";

const { managedProjects, setProjectWrite } = vi.hoisted(() => ({
  managedProjects: vi.fn(),
  setProjectWrite: vi.fn(),
}));
vi.mock("../lib/api", () => ({ api: { managedProjects, setProjectWrite } }));

import { ManagedProjectsList } from "./ToolsPanel";

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
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(h(QueryClientProvider, { client: qc }, h(ManagedProjectsList))));
  await flush();
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
});

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

describe("Managed projects ▸ access switch", () => {
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
    expect(host!.querySelector(".setup-hint")!.textContent).toContain(
      "Switch a project between read-only and read-write here; edit everything else in your config file.",
    );
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
    expect(setProjectWrite).toHaveBeenCalledWith("locked", true);
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
    expect(setProjectWrite).toHaveBeenCalledWith("open", false);
  });

  it("a no-delete row sends write:false when turned off", async () => {
    setProjectWrite.mockResolvedValue({ ok: true, name: "keeper", path: "/p/keeper", write: false });
    await mount();
    await click(sw("keeper"));
    expect(setProjectWrite).toHaveBeenCalledWith("keeper", false);
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
