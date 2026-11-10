import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkView, registerWorkSection } from "./WorkView";
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
  expect(Array.from(host.querySelectorAll("section h3")).map((n) => n.textContent)).toEqual(["Needs you", "Doing", "Waiting on", "Next", "Done"]);
  expect(host.querySelectorAll("section p")).toHaveLength(0);
  data.sources.tasks.items = [item("doing", "running")];
  await render("modal");
  expect(host.querySelectorAll("section")).toHaveLength(1);
  expect(host.textContent).toContain("Work in progress.");
});

it("defaults the rail filter on, counts other chats, and shows all on request", async () => {
  data.sources.goals.items = [item("elsewhere", "running", { kind: "goal", sessionId: "there" })];
  await render();
  expect(host.querySelector('[aria-pressed="true"]')?.textContent).toBe("This chat");
  expect(host.textContent).toContain("Nothing in this chat. 1 items in other chats.");
  await click("Show all chats");
  expect(host.querySelector('[aria-pressed="false"]')).toBeTruthy();
  expect(host.querySelector(".work-row")?.textContent).toContain("elsewhere");
  expect(useUI.getState().surfaceStates.work.filters?.thisChat).toBe(false);
});

it("defaults the modal filter off and preserves session-less items when enabled", async () => {
  data.sources.tasks.items = [item("global", "running"), item("local", "running", { sessionId: "here" }), item("remote", "running", { sessionId: "there" })];
  await render("modal");
  expect(host.querySelector('[aria-pressed="false"]')).toBeTruthy();
  await click("This chat");
  expect(host.querySelectorAll(".work-row")).toHaveLength(2);
  expect(host.textContent).not.toContain("remote");
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
  expect(host.textContent).toContain("Pick an item to see its details.");
  expect(css).toContain("@container work-view (max-width: 900px)");
});

it("renders quiet unregistered sections and uses registered renderers", async () => {
  useUI.setState({ surfaceStates: { work: { section: "goals" } } });
  await render("modal");
  expect(host.textContent).toContain("No goals to show");
  await act(async () => { dispose = registerWorkSection("goals", { label: "Goals", render: () => <p>Goal list</p> }); });
  expect(host.textContent).toContain("Goal list");
  expect(host.querySelectorAll('[role="tab"]')).toHaveLength(9);
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
