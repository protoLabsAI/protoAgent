import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkView, canonicalSection } from "./WorkView";
import { api } from "../lib/api";
import { applyNavIntent } from "../app/palette/nav";
import { useUI } from "../state/uiStore";
import type { WorkItem, WorkItems, WorkSource, WorkSourceKind } from "./useWorkItems";
import type { GroupableWorkItem } from "./tone";
import css from "./work-view.css?raw";

const data = vi.hoisted(() => ({ sources: {} as WorkItems }));
vi.mock("./useWorkItems", () => ({ useWorkItems: () => data.sources }));
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let client: QueryClient;
let root: Root;
let host: HTMLDivElement;
const kinds: WorkSourceKind[] = ["decisions", "goals", "watches", "tasks", "schedules", "background", "activity", "directives"];

/** A task row whose RAW status drives groupFor — "in_progress" lands in Doing now,
 *  "open" in Coming up — while the display status mirrors the tone map. */
function task(id: string, rawStatus = "in_progress", extra: Partial<WorkItem> = {}): WorkItem {
  return { id, kind: "task", status: rawStatus === "in_progress" ? "running" : "scheduled", title: id, meta: "Task",
    raw: { id, title: id, status: rawStatus, created_at: new Date().toISOString() }, ...extra };
}
function decision(id: string, state = "pending", extra: Partial<WorkItem> = {}): WorkItem {
  return { id, kind: "decision", status: state === "pending" ? "needs-you" : "done", title: id, meta: "",
    raw: { id, question: id, state, session_id: "", agent: "protoAgent", created_at: new Date().toISOString(), options: [] }, ...extra };
}
function goal(id: string, rawStatus = "active", extra: Partial<WorkItem> = {}): WorkItem {
  return { id, kind: "goal", status: rawStatus === "active" ? "running" : "failed", title: id, meta: "Turn 3",
    sessionId: id, raw: { session_id: id, condition: id, status: rawStatus, outcome: "" }, ...extra };
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
  await act(async () => { root.unmount(); });
  host.remove();
});

it("orders the four groups, hides empty ones, and keeps help lines off the Overview", async () => {
  data.sources.decisions.items = [decision("ask")];
  data.sources.goals.items = [goal("drive")];
  data.sources.tasks.items = [task("queued", "open")];
  data.sources.directives.items = [{ kind: "directive", id: "d1", status: "waiting", title: "Hold main", meta: "", raw: { id: "d1", status: "in_force", quote: "Hold main" } }];
  await render("modal");
  expect(Array.from(host.querySelectorAll("section h2")).map((n) => n.firstChild?.textContent))
    .toEqual(["Needs you", "Doing now", "Coming up", "Your instructions"]);
  expect(host.querySelectorAll("section p")).toHaveLength(0);
  data.sources.decisions.items = [];
  data.sources.goals.items = [];
  data.sources.directives.items = [];
  await render("modal");
  expect(Array.from(host.querySelectorAll("section h2")).map((n) => n.firstChild?.textContent)).toEqual(["Coming up"]);
});

it("shows the approved all-empty copy when every group is empty", async () => {
  await render("modal");
  expect(host.textContent).toContain("Nothing to track.");
  expect(host.textContent).toContain("Nothing needs you, nothing is running or planned, and you haven't given standing instructions.");
});

it("a picked group shows its help line and rows, or its inline empty text", async () => {
  data.sources.watches.items = [{ kind: "watch", id: "w1", status: "waiting", title: "CI goes green", meta: "Fires once when met", raw: { id: "w1", status: "active" } }];
  useUI.setState({ surfaceStates: { work: { section: "coming-up" } } });
  await render("modal");
  expect(host.querySelector(".work-view__help")?.textContent)
    .toBe("Not started yet: runs at a time, when something happens, or when it's picked up.");
  expect(host.querySelector(".work-row")?.textContent).toContain("CI goes green");
  const heading = host.querySelector("h2.work-heading")!;
  expect(heading.getAttribute("aria-label")).toBe("Coming up, 1");
  expect(heading.querySelector('[aria-hidden="true"]')?.textContent).toBe("1");
  data.sources.watches.items = [];
  await render("modal");
  expect(host.textContent).toContain("Nothing planned.");
  useUI.setState({ surfaceStates: { work: { section: "instructions" } } });
  await render("modal");
  expect(host.querySelector(".work-view__help")?.textContent).toBe("Your standing instructions and approvals, in force now.");
  expect(host.textContent).toContain("No standing instructions.");
});

it("the picker offers Overview plus the four groups, with raw single badges", async () => {
  data.sources.tasks.items = [task("run"), task("wait", "open")];
  data.sources.decisions.items = [decision("ask")];
  await render("modal");
  const menu = await openSections();
  const entries = Array.from(menu.querySelectorAll('[role="menuitemradio"]'));
  expect(entries.map((node) => node.textContent?.replace(/\d+$/, ""))).toEqual(
    ["Overview", "Needs you", "Doing now", "Coming up", "Your instructions"]);
  expect(menu.querySelectorAll(".pl-menu__badge")).toHaveLength(5);
  expect(menu.querySelector(".pl-menu__badge .pl-count")).toBeNull();
  expect(entries.map((node) => node.querySelector(".pl-menu__badge")?.textContent)).toEqual(["3", "1", "1", "1", "0"]);
  await closeSections(menu);
});

it.each([
  ["decisions", "Needs you"],
  ["goals", "Doing now"],
  ["tasks", "Doing now"],
  ["background", "Doing now"],
  ["watches", "Coming up"],
  ["schedules", "Coming up"],
  ["directives", "Your instructions"],
  ["activity", "Overview"],
  ["unheard-of", "Overview"],
] as const)("the legacy %s deep link resolves into %s", async (section, label) => {
  useUI.setState({ surfaceStates: { work: { section } } });
  await render("modal");
  expect(host.querySelector(".work-view__picker button")?.textContent).toContain(label);
  expect(canonicalSection(section)).toBe(label === "Overview" ? "overview" : canonicalSection(section));
});

it("tags every row with its kind and icon per the approved table", async () => {
  data.sources.decisions.items = [decision("ask")];
  data.sources.activity.groupItems = [{ kind: "delivery", id: "77", status: "needs-you", title: "Deploy failed page", meta: "ops",
    raw: { id: 77, priority: "now", delivered_at: null, source: "ops", text: "Deploy failed page", created_at: new Date().toISOString(), dedup_key: null } } as GroupableWorkItem];
  data.sources.goals.items = [goal("stuck", "exhausted"), goal("drive")];
  data.sources.background.items = [{ kind: "background", id: "b1", status: "running", title: "Crunching", meta: "delegate", raw: { id: "b1", status: "running" } }];
  data.sources.watches.items = [{ kind: "watch", id: "w1", status: "waiting", title: "CI goes green", meta: "", raw: { id: "w1", status: "active" } }];
  data.sources.schedules.items = [{ kind: "schedule", id: "s1", status: "scheduled", title: "Daily digest", meta: "Every day at 09:00", raw: { id: "s1", prompt: "Daily digest", schedule: "0 9 * * *" } }];
  data.sources.tasks.items = [task("queued", "open")];
  data.sources.directives.items = [{ kind: "directive", id: "d1", status: "waiting", title: "Hold main", meta: "", raw: { id: "d1", status: "in_force", quote: "Hold main" } }];
  await render("modal");
  const badges = Array.from(host.querySelectorAll(".work-row .pl-badge")).map((node) => node.textContent);
  expect(badges).toEqual(["Decision", "Goal stopped", "Delivery", "Goal", "Job", "Condition", "Job", "Time", "Instruction"]);
  // A stopped goal is neutral — never the error fill (§2, §6) — while the decision keeps info
  // and the delivery warning; the not-started job wears the dashed circle, the running one the
  // spinner, and time/condition their clock/eye glyphs.
  const row = (title: string) => Array.from(host.querySelectorAll<HTMLElement>(".work-row")).find((node) => node.textContent?.includes(title))!;
  expect(row("stuck").querySelector(".pl-badge")?.className).toBe("pl-badge");
  expect(row("ask").querySelector(".pl-badge--info")).toBeTruthy();
  expect(row("Deploy failed page").querySelector(".pl-badge--warning")).toBeTruthy();
  expect(row("queued").querySelector(".lucide-circle-dashed")).toBeTruthy();
  expect(row("queued").textContent).toContain("not started");
  expect(row("Crunching").querySelector(".pl-spinner")).toBeTruthy();
  expect(row("Daily digest").querySelector(".lucide-clock")).toBeTruthy();
  expect(row("CI goes green").querySelector(".lucide-eye")).toBeTruthy();
  expect(row("drive").querySelector(".lucide-target")).toBeTruthy();
});

it("achieved goals disappear and not-started jobs count only in Coming up", async () => {
  data.sources.goals.items = [goal("done", "achieved")];
  data.sources.tasks.items = [task("queued", "open")];
  await render("modal");
  expect(host.textContent).not.toContain("done");
  const headings = Array.from(host.querySelectorAll("section h2")).map((n) => n.firstChild?.textContent);
  expect(headings).toEqual(["Coming up"]);
  const menu = await openSections();
  const badge = (label: string) => Array.from(menu.querySelectorAll('[role="menuitemradio"]'))
    .find((node) => node.textContent?.includes(label))?.querySelector(".pl-menu__badge")?.textContent;
  expect(badge("Doing now")).toBe("0");
  expect(badge("Coming up")).toBe("1");
  await closeSections(menu);
});

it("the thisChat filter scopes groups through the shared selector, never Needs you", async () => {
  data.sources.decisions.items = [decision("remote-ask", "pending", { sessionId: "there" })];
  data.sources.tasks.items = [task("local", "in_progress", { sessionId: "here" }), task("remote", "in_progress", { sessionId: "there" })];
  useUI.setState({ surfaceStates: { work: { filters: { thisChat: true } } } });
  await render("modal");
  expect(host.textContent).toContain("remote-ask");
  expect(host.textContent).toContain("local");
  expect(host.textContent).not.toContain("remote-ask-"); // guard against suffix matches
  expect(Array.from(host.querySelectorAll(".work-row")).some((node) => node.textContent?.includes("remote") && !node.textContent.includes("remote-ask"))).toBe(false);
  useUI.setState({ surfaceStates: { work: {} } });
  await render("modal");
  expect(Array.from(host.querySelectorAll(".work-row")).filter((node) => node.textContent?.includes("remote"))).toHaveLength(2);
});

it("always lists every chat's work with no This chat / All toggle (Josh 2026-11-11)", async () => {
  data.sources.tasks.items = [task("global"), task("local", "in_progress", { sessionId: "here" }), task("remote", "in_progress", { sessionId: "there" })];
  for (const frame of ["rail", "modal"] as const) {
    await render(frame);
    expect(host.querySelector(".work-view__scope")).toBeNull();
    expect([...host.querySelectorAll("button")].some((b) => b.textContent === "This chat")).toBe(false);
    expect(host.querySelectorAll(".work-row")).toHaveLength(3);
    expect(host.textContent).toContain("remote");
  }
});

it("a wait opens the existing schedule detail; a delivery opens the delivery detail", async () => {
  data.sources.schedules.items = [{ kind: "schedule", id: "wait:w9", status: "scheduled", title: "Resume the deploy", meta: "Once",
    raw: { id: "wait:w9", prompt: "Resume the deploy", schedule: "2027-01-01T09:00", enabled: true, next_fire: "2027-01-01T09:00:00Z" } }];
  data.sources.schedules.groupItems = [{ ...data.sources.schedules.items[0], kind: "wait" } as GroupableWorkItem];
  data.sources.activity.groupItems = [{ kind: "delivery", id: "77", status: "needs-you", title: "Deploy failed page", meta: "ops",
    raw: { id: 77, priority: "now", delivered_at: null, source: "ops", text: "Deploy failed page", created_at: new Date().toISOString(), dedup_key: null } } as GroupableWorkItem];
  await render("modal");
  await act(async () => Array.from(host.querySelectorAll<HTMLButtonElement>(".work-row")).find((node) => node.textContent?.includes("Resume the deploy"))!.click());
  expect(useUI.getState().surfaceStates.work.itemId).toBe("schedule:wait:w9");
  expect(host.querySelector(".work-view__detail")?.textContent).toContain("Edit");
  await click("Back to Overview");
  await act(async () => Array.from(host.querySelectorAll<HTMLButtonElement>(".work-row")).find((node) => node.textContent?.includes("Deploy failed page"))!.click());
  const detail = host.querySelector(".work-view__detail")!;
  expect(detail.textContent).toContain("Deploy failed page");
  expect(detail.textContent).toContain("ops");
  expect(detail.querySelector(".pl-badge")?.textContent).toBe("needs you");
});

it("round-trips selection, scroll and filters across frames and back", async () => {
  data.sources.tasks.items = [task("row")];
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

it("keeps healthy groups alongside source errors, retries, and stale callouts", async () => {
  data.sources.decisions.status = "error";
  data.sources.goals.status = "stale";
  data.sources.goals.items = [goal("retained")];
  await render();
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("Couldn't load decisions");
  expect(host.textContent).toContain("Goals may be out of date");
  expect(host.querySelector(".work-row")?.textContent).toContain("retained");
  await act(async () => host.querySelector<HTMLButtonElement>('[role="alert"] button')!.click());
  expect(data.sources.decisions.retry).toHaveBeenCalledOnce();
  // A picked group only surfaces the notices for the sources that feed it.
  useUI.setState({ surfaceStates: { work: { section: "instructions" } } });
  await render();
  expect(host.querySelector('[role="alert"]')).toBeNull();
  useUI.setState({ surfaceStates: { work: { section: "needs-you" } } });
  await render();
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("Couldn't load decisions");
});

it("shows skeletons without a zero-count flash or empty announcement", async () => {
  kinds.forEach((kind) => { data.sources[kind].status = "loading"; });
  await render();
  expect(host.querySelector('[aria-busy="true"]')).toBeTruthy();
  expect(host.querySelector(".pl-count")).toBeNull();
  expect(host.textContent).not.toContain("Nothing to track");
  expect(host.querySelector('[role="status"]')?.textContent).toBe("");
});

it("omits only loading group badges while retaining ready zero counts", async () => {
  data.sources.tasks.status = "loading";
  await render();
  const nav = await openSections();
  const entry = (label: string) => Array.from(nav.querySelectorAll('[role="menuitemradio"]')).find((node) => node.textContent?.includes(label))!;
  // Tasks feed Doing now and Coming up, so those (and the Overview total) hold their badges
  // back; Needs you and Your instructions keep their ready zeros.
  expect(entry("Doing now").querySelector(".pl-menu__badge")).toBeNull();
  expect(entry("Coming up").querySelector(".pl-menu__badge")).toBeNull();
  expect(entry("Overview").querySelector(".pl-menu__badge")).toBeNull();
  expect(entry("Needs you").querySelector(".pl-menu__badge")?.textContent).toBe("0");
  expect(entry("Your instructions").querySelector(".pl-menu__badge")?.textContent).toBe("0");
  await closeSections(nav);
});

it("announces group totals and newly arriving Needs you items", async () => {
  await render("modal");
  expect(host.textContent).toContain("Nothing to track");
  expect(host.querySelector('[role="status"]')?.textContent).toBe("Overview, 0 items.");
  data.sources.decisions.items = [decision("answer")];
  await render("modal");
  expect(host.querySelector('[role="status"]')?.textContent).toBe("Overview, 1 items. 1 new Needs you items.");
});

// Importing WorkView alone must load the built-in detail renderers. Do not import
// the section modules here: that would hide a missing production registration.
it("opens the task and schedule details through the group rows", async () => {
  data.sources.tasks.items = [task("registered", "open")];
  data.sources.schedules.items = [{ kind: "schedule", id: "s1", status: "scheduled", title: "Daily digest", meta: "",
    raw: { id: "s1", prompt: "Daily digest", schedule: "0 9 * * *", enabled: true, next_fire: "2027-01-01T09:00:00Z" } }];
  useUI.setState({ surfaceStates: { work: { section: "coming-up" } } });
  await render("modal");
  for (const title of ["registered", "Daily digest"]) {
    await act(async () => Array.from(host.querySelectorAll<HTMLButtonElement>(".work-row")).find((node) => node.textContent?.includes(title))!.click());
    expect(host.querySelector(".work-view__detail")?.textContent).toContain("Edit");
    expect(host.querySelector(".work-view__detail")?.textContent).not.toContain("Details will appear here.");
    await click("Back to Coming up");
  }
});

it("the palette New goal intent opens the registered Goals creator in Doing now", async () => {
  const catalog = vi.spyOn(api, "verifiers").mockResolvedValue({ types: [], plugin_checks: [] });
  try {
    applyNavIntent({ kind: "surface", id: "work", state: { section: "goals", filters: { create: true } } });
    await render("modal");
    expect(host.querySelector(".work-view__picker button")?.textContent).toContain("Doing now");
    expect(document.querySelector('[data-testid="goal-create-dialog"]')).not.toBeNull();
  } finally {
    catalog.mockRestore();
  }
});

it("keeps the selected row current and focuses the detail heading", async () => {
  data.sources.tasks.items = [task("row")];
  useUI.setState({ surfaceStates: { work: { filters: { thisChat: false, retained: true } } } });
  await render("modal");
  await act(async () => host.querySelector<HTMLButtonElement>(".work-row")!.click());
  expect(document.activeElement).toBe(host.querySelector(".work-view__detail h2"));
  expect(host.querySelector(".work-row")?.getAttribute("aria-current")).toBe("true");
});

it("restores the originating row for legacy unqualified item ids", async () => {
  data.sources.tasks.items = [task("legacy")];
  useUI.setState({ surfaceStates: { work: { section: "tasks", itemId: "legacy" } } });
  await render();
  expect(host.querySelector(".work-row")?.getAttribute("aria-current")).toBe("true");
  await click("Back to Doing now");
  expect(document.activeElement).toBe(host.querySelector(".work-row"));
});

it("restores focus to the group row after detail", async () => {
  data.sources.goals.items = [goal("goal-row")];
  useUI.setState({ surfaceStates: { work: { section: "doing-now" } } });
  await render();
  const row = host.querySelector<HTMLButtonElement>(".work-row")!;
  await act(async () => row.click());
  expect(row.getAttribute("aria-current")).toBe("true");
  expect(document.activeElement).toBe(host.querySelector(".work-view__detail h2"));
  await click("Back to Doing now");
  expect(document.activeElement).toBe(row);
});

it("focuses a restored detail when its source finishes loading", async () => {
  data.sources.tasks.status = "loading";
  useUI.setState({ surfaceStates: { work: { section: "tasks", itemId: "task:restored" } } });
  await render();
  data.sources.tasks.status = "ready";
  data.sources.tasks.items = [task("restored")];
  await render();
  expect(document.activeElement).toBe(host.querySelector(".work-view__detail h2"));
  expect(host.querySelector(".work-row")?.getAttribute("aria-current")).toBe("true");
});

it("renders detail only on selection and Escape restores the opener without bubbling", async () => {
  data.sources.tasks.items = [task("row")];
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
    data.sources.tasks.items = [task("row")];
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
