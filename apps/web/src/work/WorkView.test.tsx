import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkView, registerWorkSection } from "./WorkView";
import { api } from "../lib/api";
import { applyNavIntent } from "../app/palette/nav";
import { useUI } from "../state/uiStore";
import type { WorkItem, WorkItems, WorkSource, WorkSourceKind } from "./useWorkItems";
import css from "./work-view.css?raw";

const data = vi.hoisted(() => ({ sources: {} as WorkItems }));
vi.mock("./useWorkItems", () => ({ useWorkItems: () => data.sources }));
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let client: QueryClient;
let root: Root;
let host: HTMLDivElement;
let dispose: (() => void) | undefined;
const kinds: WorkSourceKind[] = ["decisions", "goals", "watches", "tasks", "schedules", "background", "activity", "directives"];
function item(id: string, status: WorkItem["status"], extra: Partial<WorkItem> = {}): WorkItem {
  return { id, kind: "task", status, title: id, meta: "", raw: { title: id, prompt: id, schedule: "0 9 * * 1-5", closed_at: new Date().toISOString() }, ...extra };
}
async function render(frame: "rail" | "modal" = "rail") {
  await act(async () => root.render(<QueryClientProvider client={client}><ToastProvider><WorkView frame={frame} sessionId="here" /></ToastProvider></QueryClientProvider>));
}
async function click(text: string) {
  const button = Array.from(host.querySelectorAll("button")).find((node) => node.textContent?.trim() === text);
  expect(button, text).toBeTruthy();
  await act(async () => button!.click());
}
async function openSections() {
  const trigger = host.querySelector<HTMLButtonElement>(".work-view__picker button")!;
  await act(async () => trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
  const menu = document.querySelector<HTMLElement>('[role="menu"][aria-label="Work sections"]')!;
  expect(menu).not.toBeNull();
  return menu;
}
async function closeSections(menu: HTMLElement) {
  await act(async () => menu.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  // Radix restores menu-trigger focus after its close effect.
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
}
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  data.sources = Object.fromEntries(kinds.map((kind) => [kind, { items: [], status: "ready", error: null, loadedAt: 1, retry: vi.fn() } as WorkSource])) as WorkItems;
  useUI.setState({ surfaceStates: {}, surfaceModal: null });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); dispose?.(); });
  dispose = undefined;
  host.remove();
});

it("orders all five lanes and hides empty lanes and help lines", async () => {
  data.sources.tasks.items = [item("done", "done"), item("next", "scheduled"), item("waiting", "waiting"), item("doing", "running"), item("ask", "needs-you")];
  await render("modal");
  expect(Array.from(host.querySelectorAll("section h2")).map((n) => n.firstChild?.textContent)).toEqual(["Needs you", "Doing", "Waiting on", "Next", "Done"]);
  expect(host.querySelectorAll("section p")).toHaveLength(0);
  data.sources.tasks.items = [item("doing", "running")];
  await render("modal");
  expect(host.querySelectorAll("section")).toHaveLength(1);
  expect(host.textContent).toContain("Running now.");
});

it("always lists every chat's work, with no This chat / All toggle (Josh 2026-11-11)", async () => {
  data.sources.tasks.items = [item("global", "running"), item("local", "running", { sessionId: "here" }), item("remote", "running", { sessionId: "there" })];
  for (const frame of ["rail", "modal"] as const) {
    await render(frame);
    expect(host.querySelector(".work-view__scope")).toBeNull();
    expect([...host.querySelectorAll("button")].some((b) => b.textContent === "This chat")).toBe(false);
    expect(host.querySelectorAll(".work-row")).toHaveLength(3);
    expect(host.textContent).toContain("remote");
  }
});

it("keeps healthy lanes alongside source errors, retries, and stale callouts", async () => {
  data.sources.decisions.status = "error";
  data.sources.goals.status = "stale";
  data.sources.goals.items = [item("retained", "running")];
  await render();
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("Couldn't load decisions");
  expect(host.textContent).toContain("Goals may be out of date");
  expect(host.querySelector(".work-row")?.textContent).toContain("retained");
  await act(async () => host.querySelector<HTMLButtonElement>('[role="alert"] button')!.click());
  expect(data.sources.decisions.retry).toHaveBeenCalledOnce();
});

it("shows skeletons without a zero-count flash or empty announcement", async () => {
  kinds.forEach((kind) => { data.sources[kind].status = "loading"; });
  await render();
  expect(host.querySelector('[aria-busy="true"]')).toBeTruthy();
  expect(host.querySelector(".pl-count")).toBeNull();
  expect(host.textContent).not.toContain("Nothing going on");
  expect(host.querySelector('[role="status"]')?.textContent).toBe("");
});

it("round-trips selection, scroll and filters across frames and back", async () => {
  data.sources.tasks.items = [item("row", "running")];
  useUI.setState({ surfaceStates: { work: { section: "overview", scrollTop: 75, filters: { thisChat: false } } } });
  await render();
  const list = host.querySelector<HTMLDivElement>(".work-view__list")!;
  expect(list.scrollTop).toBe(75);
  vi.spyOn(list, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
  await act(async () => { list.scrollTop = 120; list.dispatchEvent(new Event("scroll", { bubbles: true })); });
  await act(async () => host.querySelector<HTMLButtonElement>(".work-row")!.click());
  expect(useUI.getState().surfaceStates.work.itemId).toBe("task:row");
  expect(host.textContent).toContain("Back to Overview");
  await act(async () => useUI.getState().openSurfaceModal("work"));
  await render("modal");
  expect(host.querySelector(".work-view__detail")?.textContent).toContain("row");
  expect(useUI.getState().surfaceModal?.state.scrollTop).toBe(120);
  await click("Back to Overview");
  expect(useUI.getState().surfaceModal?.state.itemId).toBeUndefined();
  expect(host.querySelector<HTMLDivElement>(".work-view__list")!.scrollTop).toBe(120);
  expect(host.querySelector(".work-view__detail")).toBeNull();
  expect(css).toContain("@container work-view (min-width: 48rem)");
  expect(css).not.toContain(".pl-");
  expect(document.activeElement).toBe(host.querySelector(".work-row"));
});

it("renders quiet unregistered sections and uses registered renderers", async () => {
  useUI.setState({ surfaceStates: { work: { section: "watches" } } });
  await render("modal");
  expect(host.textContent).toContain("No watches to show");
  await act(async () => { dispose = registerWorkSection("watches", { label: "Watches", group: "coming-up", render: () => <p>Watch list</p> }); });
  expect(host.textContent).toContain("Watch list");
  const menu = await openSections();
  expect(menu.querySelectorAll('[role="menuitemradio"]')).toHaveLength(8);
  await closeSections(menu);
});

it("summarizes disabled schedules and reveals older Done items with Show more", async () => {
  data.sources.schedules.items = [item("disabled", "paused", { kind: "schedule" })];
  data.sources.tasks.items = [item("old", "done", { raw: { closed_at: "2020-01-01T00:00:00Z" } })];
  await render("modal");
  expect(host.textContent).toContain("1 disabled schedules");
  expect(host.querySelectorAll(".work-row")).toHaveLength(0);
  await click("Show more");
  expect(host.querySelector(".work-row")?.textContent).toContain("old");
  await click("View");
  expect(useUI.getState().surfaceStates.work.section).toBe("schedules");
});

it("announces section totals and newly arriving Needs you items", async () => {
  await render("modal");
  expect(host.textContent).toContain("Nothing going on");
  expect(host.querySelector('[role="status"]')?.textContent).toBe("Overview, 0 items.");
  data.sources.decisions.items = [item("answer", "needs-you", { kind: "decision" })];
  await render("modal");
  expect(host.querySelector('[role="status"]')?.textContent).toBe("Overview, 1 items. 1 new Needs you items.");
});

// Importing WorkView alone must load the built-in section renderers. Do not import
// the section modules here: that would hide a missing production registration.
it.each(["tasks", "schedules"] as const)("loads the %s list and detail through WorkView", async (section) => {
  const kind = section === "tasks" ? "task" : "schedule";
  data.sources[section].items = [item("registered", "scheduled", { kind })];
  useUI.setState({ surfaceStates: { work: { section } } });
  await render("modal");
  expect(host.querySelector(".work-row__title")?.textContent).toBe("registered");
  await act(async () => host.querySelector<HTMLButtonElement>(".work-row")!.click());
  expect(host.querySelector(".work-view__detail")?.textContent).toContain("Edit");
  expect(host.querySelector(".work-view__detail")?.textContent).not.toContain("Details will appear here.");
});

it("the palette New goal intent opens the registered Goals creator in Work", async () => {
  const catalog = vi.spyOn(api, "verifiers").mockResolvedValue({ types: [], plugin_checks: [] });
  try {
    applyNavIntent({ kind: "surface", id: "work", state: { section: "goals", filters: { create: true } } });
    await render("modal");
    expect(document.querySelector('[data-testid="goal-create-dialog"]')).not.toBeNull();
  } finally {
    catalog.mockRestore();
  }
});

it("passes raw counts including zero to SideNav and keeps the selected row current", async () => {
  data.sources.tasks.items = [item("row", "running")];
  useUI.setState({ surfaceStates: { work: { filters: { thisChat: false, retained: true } } } });
  await render("modal");
  const nav = await openSections();
  expect(nav.querySelectorAll(".pl-menu__badge")).toHaveLength(8);
  expect(nav.querySelector(".pl-menu__badge .pl-count")).toBeNull();
  expect(Array.from(nav.querySelectorAll(".pl-menu__badge")).map((node) => node.textContent)).toContain("0");
  await closeSections(nav);
  await act(async () => host.querySelector<HTMLButtonElement>(".work-row")!.click());
  expect(document.activeElement).toBe(host.querySelector(".work-view__detail h2"));
  expect(host.querySelector(".work-row")?.getAttribute("aria-current")).toBe("true");
});

it("omits only loading badges while retaining ready zero counts", async () => {
  data.sources.tasks.status = "loading";
  await render();
  const nav = await openSections();
  const tabs = Array.from(nav.querySelectorAll('[role="menuitemradio"]'));
  expect(tabs.find((node) => node.textContent === "Tasks")?.querySelector(".pl-menu__badge")).toBeNull();
  expect(tabs.find((node) => node.textContent === "Overview")?.querySelector(".pl-menu__badge")).toBeNull();
  expect(nav.querySelectorAll(".pl-menu__badge")).toHaveLength(6);
  expect(Array.from(nav.querySelectorAll(".pl-menu__badge")).every((node) => node.textContent === "0")).toBe(true);
  await closeSections(nav);
});

it("counts only the rows the section lists — finished jobs and achieved goals don't badge", async () => {
  data.sources.tasks.items = [
    item("open-job", "scheduled", { raw: { status: "open", created_at: "2026-11-01T00:00:00Z" } }),
    item("closed-job", "done", { raw: { status: "closed", closed_at: "2026-11-01T00:00:00Z" } }),
  ];
  data.sources.goals.items = [item("done-goal", "done", { kind: "goal", raw: { session_id: "g", status: "achieved", outcome: "done-goal" } })];
  useUI.setState({ surfaceStates: { work: { section: "tasks" } } });
  await render("modal");
  // The heading, the live announcement and the list agree: the closed job has left Work.
  expect(host.querySelector(".work-heading")?.getAttribute("aria-label")).toBe("Tasks, 1");
  expect(host.querySelector('[role="status"]')?.textContent).toBe("Tasks, 1 items.");
  expect(host.querySelectorAll(".work-row")).toHaveLength(1);
  const nav = await openSections();
  const badge = (label: string) => Array.from(nav.querySelectorAll('[role="menuitemradio"]'))
    .find((node) => node.textContent?.startsWith(label))?.querySelector(".pl-menu__badge")?.textContent;
  expect(badge("Tasks")).toBe("1");
  expect(badge("Goals")).toBe("0"); // only an achieved goal → the badge matches its "No goals" empty state
  await closeSections(nav);
});

it("restores the originating row for legacy unqualified item ids", async () => {
  data.sources.tasks.items = [item("legacy", "running")];
  useUI.setState({ surfaceStates: { work: { section: "tasks", itemId: "legacy" } } });
  await render();
  expect(host.querySelector(".work-row")?.getAttribute("aria-current")).toBe("true");
  await click("Back to Tasks");
  expect(document.activeElement).toBe(host.querySelector(".work-row"));
});

it("restores focus to the Goals section row after detail", async () => {
  data.sources.goals.items = [item("goal-row", "running", {
    kind: "goal", raw: { session_id: "here", status: "active", outcome: "Goal row" },
  })];
  useUI.setState({ surfaceStates: { work: { section: "goals" } } });
  await render();
  const row = host.querySelector<HTMLButtonElement>(".work-row")!;
  await act(async () => row.click());
  expect(row.getAttribute("aria-current")).toBe("true");
  expect(document.activeElement).toBe(host.querySelector(".work-view__detail h2"));
  await click("Back to Goals");
  expect(document.activeElement).toBe(row);
});

it("focuses a restored detail when its source finishes loading", async () => {
  data.sources.tasks.status = "loading";
  useUI.setState({ surfaceStates: { work: { section: "tasks", itemId: "task:restored" } } });
  await render();
  data.sources.tasks.status = "ready";
  data.sources.tasks.items = [item("restored", "running")];
  await render();
  expect(document.activeElement).toBe(host.querySelector(".work-view__detail h2"));
  expect(host.querySelector(".work-row")?.getAttribute("aria-current")).toBe("true");
});

it("renders detail only on selection and Escape restores the opener without bubbling", async () => {
  data.sources.tasks.items = [item("row", "running")];
  await render("modal");
  expect(host.querySelector(".work-view__detail")).toBeNull();
  const row = host.querySelector<HTMLButtonElement>(".work-row")!;
  expect(row.closest("li")?.parentElement?.tagName).toBe("UL");
  await act(async () => row.click());
  expect(row.getAttribute("aria-current")).toBe("true");
  const escape = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
  const bubble = vi.fn();
  window.addEventListener("keydown", bubble);
  await act(async () => host.querySelector(".work-view__detail h2")!.dispatchEvent(escape));
  window.removeEventListener("keydown", bubble);
  expect(bubble).not.toHaveBeenCalled();
  expect(escape.defaultPrevented).toBe(true);
  expect(host.querySelector(".work-view__detail")).toBeNull();
  expect(document.activeElement).toBe(row);
});

it.each([
  ["needs-you", "Decisions waiting for your answer."],
  ["running", "Running now."],
  ["waiting", "Paused until something happens."],
  ["scheduled", "Scheduled or queued."],
  ["done", "The last 24 hours."],
] as const)("shows exact single-lane help for %s", async (status, help) => {
  data.sources.tasks.items = [item("one", status), item("two", status)];
  await render();
  expect(host.querySelector("section p")?.textContent).toBe(help);
  const heading = host.querySelector("section h2")!;
  expect(heading.getAttribute("aria-label")).toBe(`${heading.firstChild?.textContent}, 2`);
  expect(heading.querySelector('[aria-hidden="true"]')?.textContent).toBe("2");
  expect(heading.querySelector(".work-view__announcement")?.textContent).toBe(", 2");
  expect(host.querySelectorAll("section ul > li > button.work-row")).toHaveLength(2);
});

it("ignores resize-reset scroll events until the observer restores the saved position", async () => {
  const original = globalThis.ResizeObserver;
  let resize = () => {};
  class Observer {
    constructor(private callback: ResizeObserverCallback) {}
    observe(target: Element) {
      if (target.classList.contains("work-view__list")) resize = () => this.callback([], this as unknown as ResizeObserver);
    }
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", Observer);
  try {
    data.sources.tasks.items = [item("row", "running")];
    useUI.setState({ surfaceStates: { work: { scrollTop: 75 } } });
    await render("modal");
    const list = host.querySelector<HTMLDivElement>(".work-view__list")!;
    let width = 1000;
    Object.defineProperty(list, "clientWidth", { get: () => width });
    Object.defineProperty(list, "clientHeight", { get: () => 500 });
    vi.spyOn(list, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
    resize();
    width = 360;
    await act(async () => {
      list.scrollTop = 0;
      list.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    expect(useUI.getState().surfaceStates.work.scrollTop).toBe(75);
    resize();
    expect(list.scrollTop).toBe(75);
  } finally {
    vi.stubGlobal("ResizeObserver", original);
  }
});
