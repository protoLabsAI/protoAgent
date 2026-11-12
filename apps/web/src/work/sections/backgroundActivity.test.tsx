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
import { sections } from "../sectionRegistry";
import { useUI } from "../../state/uiStore";
import type { WorkItem, WorkItems, WorkSource, WorkSourceKind } from "../useWorkItems";
// Importing the sections registers them in the WorkView registry (module side effect).
import { roleLabel } from "./background";
import { triggeredByLabel, isUrgent } from "./activity";

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
function actItem(id: string, status: WorkItem["status"], title: string, raw: Record<string, unknown>): WorkItem {
  return { kind: "activity", id, status, title, meta: "", raw };
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

it("labels every activity trigger in plain words and flags only the inbox now tier", () => {
  expect(triggeredByLabel("scheduler")).toBe("Scheduled");
  expect(triggeredByLabel("watch")).toBe("Scheduled");
  expect(triggeredByLabel("inbox")).toBe("Inbox");
  expect(triggeredByLabel("webhook")).toBe("Webhook");
  expect(triggeredByLabel("a2a")).toBe("Another agent");
  expect(triggeredByLabel("operator")).toBe("You");
  expect(triggeredByLabel("")).toBe("You");
  expect(isUrgent({ priority: "now" })).toBe(true);
  expect(isUrgent({ priority: "next" })).toBe(false);
  expect(isUrgent({ priority: "" })).toBe(false);
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

it("opens the existing SubagentsPanel from the Manage subagents link", async () => {
  mocks.sources.background.items = [bgItem("c1", "done", { id: "c1", status: "completed", subagent_type: "delegate", description: "Summarize" })];
  await render("background");
  expect(document.querySelector('[data-testid="subagents-panel"]')).toBeNull();
  await click("Manage subagents");
  expect(document.querySelector('[data-testid="subagents-panel"]')).toBeTruthy();
});

// ── Activity (§2.10) ────────────────────────────────────────────────────────────
it("shows only the last 24 h until Show more reveals older rows", async () => {
  const recent = new Date(Date.now() - 1000).toISOString();
  mocks.sources.activity.items = [
    actItem("a-new", "done", "a-new-turn", { id: 1, origin: "scheduler", state: "completed", created_at: recent }),
    actItem("a-old", "done", "a-old-turn", { id: 2, origin: "scheduler", state: "completed", created_at: "2020-01-01T00:00:00Z" }),
  ];
  await render("activity");
  expect(host.textContent).toContain("a-new-turn");
  expect(host.textContent).not.toContain("a-old-turn");
  await click("Show more");
  expect(host.textContent).toContain("a-old-turn");
  expect(host.querySelectorAll("ul.work-group > li > button.work-row")).toHaveLength(2);
});

it("shows Urgent only for the inbox now tier", async () => {
  mocks.sources.activity.items = [
    actItem("u1", "done", "urgent-turn", { id: 1, origin: "inbox", priority: "now", state: "completed", created_at: new Date().toISOString() }),
    actItem("u2", "done", "calm-turn", { id: 2, origin: "inbox", priority: "later", state: "completed", created_at: new Date().toISOString() }),
  ];
  await render("activity", "activity:u1");
  expect(fieldValue("Triggered by")).toBe("Inbox");
  expect(host.textContent).toContain("Urgent");
  await render("activity", "activity:u2");
  expect(host.textContent).not.toContain("Urgent");
});

it("places background adapters in Doing now without giving activity a Work group", () => {
  expect(sections.get("background")?.group).toBe("doing-now");
  expect(sections.get("activity")).toBeDefined();
  expect(sections.get("activity")?.group).toBeUndefined();
});
