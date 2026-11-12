import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

// DS Drawer (Radix focus-trap) and Badge touch layout/pointer APIs jsdom lacks.
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.releasePointerCapture ??= () => {};
Element.prototype.setPointerCapture ??= () => {};
Element.prototype.scrollIntoView = vi.fn();
if (!(globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver) {
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
if (!window.matchMedia) {
  window.matchMedia = ((q: string) =>
    ({ matches: false, media: q, onchange: null, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false })) as typeof window.matchMedia;
}

const mocks = vi.hoisted(() => ({
  sources: {} as import("../useWorkItems").WorkItems,
  stopBackground: vi.fn(),
  switchSession: vi.fn(),
  sessions: [] as { id: string; title: string }[],
}));

// The one data hook — fed directly, so no queries run (mirrors WorkView.test.tsx).
vi.mock("../useWorkItems", () => ({ useWorkItems: () => mocks.sources }));
vi.mock("../../lib/api", () => ({ api: { stopBackground: mocks.stopBackground, inbox: async () => ({ items: [] }) } }));
vi.mock("../../lib/events", () => ({
  onServerEvent: () => () => {},
  onTopic: () => () => {},
  onConnectionChange: () => () => {},
  isConnected: () => true,
}));
vi.mock("../../chat/chat-store", () => ({
  chatStore: { switchSession: mocks.switchSession },
  useChatState: () => ({ sessions: mocks.sessions }),
}));
// The heavy lazy Markdown pipeline — render children as plain text for the clamp test.
vi.mock("../../chat/LazyMarkdown", () => ({
  Markdown: ({ children }: { children: ReactNode }) => <div className="md">{children}</div>,
}));
// The existing panel the "Manage subagents" link opens — kept separate (Q14).
vi.mock("../../app/SubagentsPanel", () => ({
  SubagentsPanel: () => <div data-testid="subagents-panel">Subagents roster</div>,
}));

import { ToastProvider } from "@protolabsai/ui/overlays";
import { WorkView } from "../WorkView";
import { registerWorkSection, sections, type WorkSection } from "../sectionRegistry";
import { useUI } from "../../state/uiStore";
import type { WorkItem, WorkItems, WorkSource, WorkSourceKind } from "../useWorkItems";
// The production barrel (S4c): importing it registers every built-in section — and
// proves Activity is NOT among them (its renderer is deleted, not merely unimported).
import "./index";
import { roleLabel } from "./background";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const kinds: WorkSourceKind[] = ["decisions", "goals", "watches", "tasks", "schedules", "background", "activity", "directives"];
let root: Root;
let host: HTMLDivElement;

function emptySources(): WorkItems {
  return Object.fromEntries(
    kinds.map((kind) => [kind, { items: [], status: "ready", error: null, loadedAt: 1, retry: vi.fn() } as WorkSource]),
  ) as WorkItems;
}
function bgItem(id: string, status: WorkItem["status"], raw: Record<string, unknown>): WorkItem {
  return { kind: "background", id, status, title: String(raw.description || id), meta: String(raw.subagent_type || ""), raw };
}
async function render(section: string, itemId?: string) {
  useUI.setState({ surfaceStates: { work: { section, itemId, filters: { thisChat: false } } }, surfaceModal: null });
  await act(async () => root.render(<ToastProvider><WorkView frame="modal" sessionId="here" /></ToastProvider>));
}
function button(text: string): HTMLButtonElement | undefined {
  return Array.from(document.querySelectorAll("button")).find((n) => n.textContent?.trim() === text) as HTMLButtonElement | undefined;
}
async function click(text: string) {
  const b = button(text);
  expect(b, text).toBeTruthy();
  await act(async () => b!.click());
}
/** The `dd` text of a WorkDetail field addressed by its `dt` label. */
function fieldValue(label: string): string | undefined {
  const field = Array.from(host.querySelectorAll(".work-detail__field")).find(
    (f) => f.querySelector("dt")?.textContent?.trim() === label,
  );
  return field?.querySelector("dd")?.textContent?.trim();
}

beforeEach(() => {
  mocks.sources = emptySources();
  mocks.stopBackground.mockReset().mockResolvedValue({ ok: true });
  mocks.switchSession.mockReset();
  mocks.sessions = [];
  useUI.setState({ surfaceStates: {}, surfaceModal: null });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

// ── pure helpers ──────────────────────────────────────────────────────────────
it("maps a subagent slug to a display name, never the raw slug", () => {
  expect(roleLabel("research-assistant")).toBe("Research Assistant");
  expect(roleLabel("delegate")).toBe("Delegate");
  expect(roleLabel("")).toBe("Subagent");
});

// ── Background agents (§2.9) ────────────────────────────────────────────────────
it("shows Stopped: reason and the display-name role for a failed job", async () => {
  mocks.sources.background.items = [
    bgItem("f1", "failed", { id: "f1", status: "failed", subagent_type: "research-assistant", error: "ran out of budget", description: "Dig the logs" }),
  ];
  await render("background", "background:f1");
  expect(fieldValue("Role")).toBe("Research Assistant");
  expect(host.textContent).toContain("Stopped: ran out of budget");
});

it("does not show Why it ended for a completed job", async () => {
  mocks.sources.background.items = [
    bgItem("c1", "done", { id: "c1", status: "completed", subagent_type: "delegate", result: "All done", description: "Summarize" }),
  ];
  await render("background", "background:c1");
  expect(fieldValue("Role")).toBe("Delegate");
  expect(host.textContent).not.toContain("Stopped:");
});

it("clamps a long result until Show full text is pressed", async () => {
  mocks.sources.background.items = [
    bgItem("c1", "done", { id: "c1", status: "completed", subagent_type: "delegate", result: "The full report body", description: "Summarize" }),
  ];
  await render("background", "background:c1");
  expect(host.textContent).toContain("The full report body");
  await click("Show full text");
  expect(button("Show less")).toBeTruthy();
});

it("confirms before cancelling, then calls the cancel route", async () => {
  mocks.sources.background.items = [
    bgItem("r1", "running", { id: "r1", status: "running", subagent_type: "delegate", description: "Crunching" }),
  ];
  await render("background", "background:r1");
  expect(mocks.stopBackground).not.toHaveBeenCalled();
  await click("Cancel job…");
  // The §4.4 confirm appears first — the route is untouched until it is accepted.
  expect(host.textContent).toContain("Cancel this background job?");
  expect(mocks.stopBackground).not.toHaveBeenCalled();
  await click("Cancel job");
  expect(mocks.stopBackground).toHaveBeenCalledWith("r1");
});

it("offers no Cancel job action on a finished job", async () => {
  mocks.sources.background.items = [
    bgItem("c1", "done", { id: "c1", status: "completed", subagent_type: "delegate", result: "done", description: "Summarize" }),
  ];
  await render("background", "background:c1");
  expect(button("Cancel job…")).toBeUndefined();
});

it("links From {chat title} to an open chat without showing the session id", async () => {
  mocks.sessions = [{ id: "sess-123", title: "Logs deep-dive" }];
  mocks.sources.background.items = [
    bgItem("c1", "done", { id: "c1", status: "completed", subagent_type: "delegate", origin_session: "sess-123", result: "done", description: "Summarize" }),
  ];
  await render("background", "background:c1");
  expect(fieldValue("From")).toBe("Logs deep-dive");
  expect(host.textContent).not.toContain("sess-123");
  await click("Logs deep-dive");
  expect(mocks.switchSession).toHaveBeenCalledWith("sess-123");
});

it("a background deep link lands on Doing now and the row opens the preserved §2.9 detail", async () => {
  // The old Background agents list (and its Manage subagents footer) is gone with the
  // four-group consolidation (v2 §7.2); the per-kind DETAIL renderer is what survives.
  mocks.sources.background.items = [bgItem("r1", "running", { id: "r1", status: "running", subagent_type: "research-assistant", description: "Dig the logs" })];
  await render("background");
  expect(host.querySelector(".work-view__picker button")?.textContent).toContain("Doing now");
  expect(button("Manage subagents")).toBeUndefined();
  const row = host.querySelector<HTMLButtonElement>(".work-row")!;
  expect(row.querySelector(".pl-badge")?.textContent).toBe("Job");
  await act(async () => row.click());
  expect(fieldValue("Role")).toBe("Research Assistant");
});

// ── Activity is no longer a Work section (S4c; history is not Work, v2 §1) ──────
it("registers every built-in with its canonical group and leaves Activity out of the registry", () => {
  expect(sections.get("background")?.group).toBe("doing-now");
  for (const [id, section] of sections) {
    expect(section.group, id).toMatch(/^(needs-you|doing-now|coming-up|instructions)$/);
  }
  expect(sections.has("activity")).toBe(false);
});

it("lands an old Activity deep-link on the Overview, never a feed — history rows stay out of Work", async () => {
  // A history row in the activity source (its production items stay empty) must not render.
  mocks.sources.activity.items = [{ kind: "activity", id: "a-new", status: "done", title: "a-new-turn",
    meta: "", raw: { id: 1, origin: "scheduler", state: "completed", created_at: new Date().toISOString() } }];
  await render("activity");
  expect(host.querySelector(".work-view__picker button")?.textContent).toContain("Overview");
  expect(host.textContent).toContain("Nothing to track.");
  expect(host.textContent).not.toContain("a-new-turn");
  expect(document.querySelector('[data-testid="unified-feed"]')).toBeNull();
  // The picker offers no Activity destination either — Overview plus the four groups (v2 §4).
  const trigger = host.querySelector<HTMLButtonElement>(".work-view__picker button")!;
  await act(async () => trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
  const menu = document.querySelector<HTMLElement>('[role="menu"][aria-label="Work sections"]')!;
  const labels = Array.from(menu.querySelectorAll('[role="menuitemradio"]')).map((n) => n.textContent ?? "");
  expect(labels).toHaveLength(5);
  expect(labels.some((label) => label.includes("Activity"))).toBe(false);
  await act(async () => menu.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
});

it("rejects re-registering Activity as an ungrouped Work section", () => {
  expect(() =>
    registerWorkSection("activity", { label: "Activity", render: () => null } as unknown as WorkSection),
  ).toThrow(/group/);
  expect(sections.has("activity")).toBe(false);
});
