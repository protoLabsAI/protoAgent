import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NeedsYouSection } from "./NeedsYouSection";
import { openWorkGroup } from "./ChatStatusRail";
import { WorkView } from "../work/WorkView";
import type { WorkItem, WorkItems, WorkSource } from "../work/useWorkItems";
import type { GroupableWorkItem } from "../work/tone";
import type { DecisionRow } from "../lib/api/decisions";
import { beginThisChatVisit, endThisChatVisit } from "../work/visitScope";
import { useUI } from "../state/uiStore";
import { api } from "../lib/api";
import { setPaletteNavigator } from "../app/palette/nav";

const data = vi.hoisted(() => ({ sources: {} as WorkItems }));
const openChat = vi.hoisted(() => vi.fn());
vi.mock("../work/useWorkItems", () => ({ useWorkItems: () => data.sources }));
vi.mock("../work/openSourceChat", () => ({ openSourceChat: openChat }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  endThisChatVisit();
  useUI.setState({ surfaceStates: {}, surfaceModal: null });
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  openChat.mockClear();
});

it.each(["agent-handoff", undefined])("opens the shared answer detail for source %s without a chat error", async (source) => {
  const item: WorkItem = {
    kind: "task", id: "waiting", status: "needs-you", title: "Ship release", meta: "", sessionId: source,
    raw: { id: "waiting", title: "Ship release", status: "waiting", session_id: source,
      ask: "Can we ship?", links: [{ ref: "https://example.com/pull/7" }] },
  };
  data.sources = Object.fromEntries(
    ["decisions", "goals", "watches", "tasks", "schedules", "background", "activity", "directives"]
      .map((kind) => [kind, { items: kind === "tasks" ? [item] : [], status: "ready",
        error: null, loadedAt: 1, retry: vi.fn() } as WorkSource]),
  ) as WorkItems;
  vi.spyOn(api, "chatSessionTurns").mockResolvedValue({ turns: [] });
  useUI.setState({ surfaceStates: {}, surfaceModal: null });
  beginThisChatVisit(); // A previous chat-scoped visit must not hide this fleet-wide item.
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onNavigate = vi.fn();
  try {
    await act(async () => root.render(
      <QueryClientProvider client={client}><ToastProvider>
        <NeedsYouSection items={[item]} held={new Map()} onHold={vi.fn()} records={{}}
          answer={vi.fn()} mobile={false} onNavigate={onNavigate}
          openWork={(itemId) => openWorkGroup("needs-you", itemId)} goalsRetry={vi.fn()} />
        <WorkView frame="rail" sessionId="unrelated-chat" />
      </ToastProvider></QueryClientProvider>,
    ));
    await act(async () => host.querySelector<HTMLButtonElement>(".chat-needs-you .chat-status-row")!.click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(onNavigate).toHaveBeenCalledOnce();
    const detail = host.querySelector(".work-detail")!;
    expect(detail).toBeTruthy();
    expect(detail.textContent).toContain("Can we ship?");
    expect(detail.querySelector("a")?.getAttribute("href")).toBe("https://example.com/pull/7");
    // Revision 5 (§11.1 item 6): an A2A context the store doesn't list links out as "Open the
    // conversation"; with no source at all the field drops out entirely.
    expect(detail.querySelector('a[href^="#/conversations/"]')?.textContent ?? null).toBe(source ? "Open the conversation" : null);
    const labels = [...detail.querySelectorAll("button")].map((button) => button.textContent);
    expect(labels).toEqual(expect.arrayContaining(["Send reply", "Done…", "Dismiss…"]));
    expect(labels).not.toContain("Open chat");
    expect(openChat).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain("Couldn't");
    expect(document.querySelector(".pl-toast")).toBeNull();
  } finally {
    await act(async () => root.unmount());
    client.clear();
    host.remove();
  }
});

// ── S3a: the body restyled into the §1 quiet frame ────────────────────────────────────────────
describe("the quiet-frame rows (S3a)", () => {
  // Only the fields NeedsYouSection / DecisionCard read; the rest read as absent.
  const decisionRow = (id: string, extra: Partial<DecisionRow> = {}): DecisionRow => ({
    id, session_id: `chat-${id}`, agent: "designSystem", question: `Decide ${id}`, state: "pending",
    options: [{ id: "a", label: "Go", recommended: true }, { id: "b", label: "Wait" }],
    created_at: new Date().toISOString(), default_action_text: "carry on without this step", ...extra,
  } as DecisionRow);
  const decisionItem = (r: DecisionRow): GroupableWorkItem => ({
    kind: "decision", id: r.id, status: "needs-you", title: r.question, meta: "", sessionId: r.session_id, raw: r,
  });
  const deliveryItem = (id: number): GroupableWorkItem => ({
    kind: "delivery", id: String(id), status: "needs-you", title: `Page ${id}`, meta: "mail",
    raw: { id, text: `Page ${id}`, source: "slack", priority: "now", delivered_at: null, needs_human: true } as never,
  });
  const goalItem = (id: string): GroupableWorkItem => ({
    kind: "goal", id, status: "failed", title: `Goal ${id}`, meta: "", sessionId: id,
    raw: { session_id: id, status: "exhausted" } as never,
  });
  const waitingTask = (id: string, agoMin: number): GroupableWorkItem => ({
    kind: "task", id, status: "needs-you", title: "Ship the release", meta: "", sessionId: "other-chat",
    raw: { id, title: "Ship the release", status: "waiting",
      updated_at: new Date(Date.now() - agoMin * 60_000).toISOString() } as never,
  });

  const findButton = (scope: ParentNode, match: (text: string) => boolean) =>
    [...scope.querySelectorAll("button")].find((b) => match(b.textContent ?? ""));
  const flush = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });

  type SectionProps = Parameters<typeof NeedsYouSection>[0];
  async function renderSection(overrides: Partial<SectionProps> & { items: GroupableWorkItem[] }) {
    const host = document.createElement("div");
    document.body.append(host);
    const root: Root = createRoot(host);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const props: SectionProps = {
      held: new Map<string, DecisionRow>(), onHold: vi.fn(), records: {}, answer: vi.fn(),
      mobile: false, onNavigate: vi.fn(), openWork: vi.fn(), goalsRetry: vi.fn(), ...overrides,
    };
    await act(async () => root.render(
      <QueryClientProvider client={client}><ToastProvider>
        <NeedsYouSection {...props} />
      </ToastProvider></QueryClientProvider>,
    ));
    return {
      host,
      section: () => host.querySelector<HTMLElement>(".chat-needs-you")!,
      cleanup: async () => { await act(async () => root.unmount()); client.clear(); host.remove(); },
    };
  }

  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    Element.prototype.hasPointerCapture = Element.prototype.hasPointerCapture ?? (() => false);
    Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});
  });

  // r1
  it("renders the first pending decision as a DecisionCard whose answer routes through the store", async () => {
    const answer = vi.fn().mockResolvedValue({ answered_elsewhere: false, resumed: true,
      decision: decisionRow("q1", { state: "answered", answer: "a", answer_label: "Go" }) });
    const { section, cleanup } = await renderSection({ items: [decisionItem(decisionRow("q1"))], answer });
    try {
      expect(section().querySelectorAll('[role="group"][aria-label="Answer"]')).toHaveLength(1);
      expect(section().querySelector(".chat-status-row")).toBeNull(); // the sole decision is the expanded card
      expect(section().querySelector(".pl-card")).toBeNull(); // the row is the container (#5668 §1)
      await act(async () => findButton(section(), (t) => t.startsWith("Go"))!.click());
      await flush();
      expect(answer).toHaveBeenCalledWith("q1", "a"); // the shared store's answer, not a fork
    } finally { await cleanup(); }
  });

  // r2
  it("renders a second decision as a quiet '<agent> · asked <age>' row that expands into a card", async () => {
    const items = [decisionItem(decisionRow("q1")), decisionItem(decisionRow("q2"))];
    const { section, cleanup } = await renderSection({ items });
    try {
      expect(section().querySelectorAll('[role="group"][aria-label="Answer"]')).toHaveLength(1);
      const row = section().querySelector<HTMLButtonElement>(".chat-status-row[aria-expanded]")!;
      expect(row.getAttribute("aria-expanded")).toBe("false");
      expect(row.querySelector(".chat-status-row__text")!.textContent).toBe("Decide q2");
      expect(row.querySelector(".chat-status-row__cue")!.textContent).toContain("designSystem · asked");
      await act(async () => row.click());
      expect(section().querySelectorAll('[role="group"][aria-label="Answer"]')).toHaveLength(2);
    } finally { await cleanup(); }
  });

  // r3
  it("renders a waiting task as a quiet row that opens its Work detail + closes the panel, with no per-row button", async () => {
    const onNavigate = vi.fn();
    const openWork = vi.fn();
    const { section, cleanup } = await renderSection({ items: [waitingTask("t1", 5)], onNavigate, openWork });
    try {
      const li = section().querySelector("li")!;
      const row = li.querySelector<HTMLButtonElement>(".chat-status-row")!;
      expect(li.querySelectorAll("button")).toHaveLength(1); // the row IS the control — no "Open in Work" per row
      expect(row.querySelector(".chat-status-row__text")!.textContent).toBe("Ship the release");
      expect(row.querySelector(".chat-status-row__cue")!.textContent).toBe("Waiting on you · 5 min ago");
      await act(async () => row.click());
      expect(onNavigate).toHaveBeenCalledOnce(); // closes the panel
      expect(openWork).toHaveBeenCalledWith("task:t1");
    } finally { await cleanup(); }
  });

  // r4
  it("renders an undelivered page as a quiet row that opens the Needs you group", async () => {
    const onNavigate = vi.fn();
    const openWork = vi.fn();
    const { section, cleanup } = await renderSection({ items: [deliveryItem(71)], onNavigate, openWork });
    try {
      const row = section().querySelector<HTMLButtonElement>(".chat-status-row")!;
      expect(row.querySelector(".chat-status-row__text")!.textContent).toBe("Page 71");
      expect(row.querySelector(".chat-status-row__cue")!.textContent).toBe("slack couldn't be delivered");
      await act(async () => row.click());
      expect(onNavigate).toHaveBeenCalledOnce();
      expect(openWork).toHaveBeenCalledWith(); // no itemId → the Needs you group
    } finally { await cleanup(); }
  });

  // r4
  it("keeps the stopped goal's Keep going / Drop the goal controls with the inline confirm", async () => {
    const goalsRetry = vi.fn();
    vi.spyOn(api, "rearmGoal").mockResolvedValue({ message: "ok" } as never);
    vi.spyOn(api, "clearGoal").mockResolvedValue({} as never);
    const { section, cleanup } = await renderSection({ items: [goalItem("g1")], goalsRetry });
    try {
      const goal = section().querySelector<HTMLElement>(".chat-needs-you__goal")!;
      expect(goal.querySelector(".chat-status-row__text")!.textContent).toBe("Goal g1");
      expect(goal.querySelector(".chat-status-row__cue")!.textContent).toBe("Goal stopped");
      await act(async () => findButton(goal, (t) => t === "Keep going")!.click());
      await flush();
      expect(api.rearmGoal).toHaveBeenCalledWith("g1", {});
      expect(goalsRetry).toHaveBeenCalledTimes(1);
      await act(async () => findButton(goal, (t) => t === "Drop the goal")!.click());
      expect(api.clearGoal).not.toHaveBeenCalled(); // the first click only arms the confirm
      expect(goal.textContent).toContain("Drop this goal?");
      await act(async () => findButton(goal, (t) => t === "Drop the goal")!.click());
      await flush();
      expect(api.clearGoal).toHaveBeenCalledWith("g1", false);
      expect(goalsRetry).toHaveBeenCalledTimes(2);
    } finally { await cleanup(); }
  });

  // ── C6b: Open conversation for non-chat threads ─────────────────────────────────────────────
  const waitingFrom = (id: string, sessionId: string | undefined): GroupableWorkItem => ({
    kind: "task", id, status: "needs-you", title: "Ship the release", meta: "", sessionId,
    raw: { id, title: "Ship the release", status: "waiting", session_id: sessionId,
      updated_at: new Date().toISOString() } as never,
  });

  // r1
  it("a waiting task from a non-chat A2A context gets an Open conversation link that opens the thread and closes the panel", async () => {
    const nav = vi.fn();
    setPaletteNavigator(nav);
    const onNavigate = vi.fn();
    const openWork = vi.fn();
    const { section, cleanup } = await renderSection({ items: [waitingFrom("t1", "ctx-9")], onNavigate, openWork });
    try {
      const li = section().querySelector<HTMLElement>("li.chat-needs-you__task")!;
      const open = findButton(li, (t) => t === "Open conversation")!;
      expect(open).toBeTruthy();
      // The row still opens its Work detail in place — the trailing link doesn't swallow its click.
      await act(async () => li.querySelector<HTMLButtonElement>(".chat-status-row")!.click());
      expect(openWork).toHaveBeenCalledWith("task:t1");
      expect(nav).not.toHaveBeenCalled();
      // The link closes the panel and deep-links to the read-only thread (C6a's route).
      onNavigate.mockClear();
      await act(async () => open.click());
      expect(onNavigate).toHaveBeenCalledOnce();
      expect(nav).toHaveBeenCalledWith({ kind: "conversation", contextId: "ctx-9" });
    } finally { setPaletteNavigator(null); await cleanup(); }
  });

  // r1
  it("a pending decision from a non-chat A2A context gets an Open conversation link beside its card", async () => {
    const nav = vi.fn();
    setPaletteNavigator(nav);
    const onNavigate = vi.fn();
    const items = [decisionItem(decisionRow("q1", { session_id: "ctx-7" }))];
    const { section, cleanup } = await renderSection({ items, onNavigate });
    try {
      const card = section().querySelector<HTMLElement>(".chat-needs-you__card")!;
      expect(card.querySelector('[role="group"][aria-label="Answer"]')).toBeTruthy(); // beside the answer area
      const open = findButton(card, (t) => t === "Open conversation")!;
      expect(open).toBeTruthy();
      await act(async () => open.click());
      expect(onNavigate).toHaveBeenCalledOnce();
      expect(nav).toHaveBeenCalledWith({ kind: "conversation", contextId: "ctx-7" });
    } finally { setPaletteNavigator(null); await cleanup(); }
  });

  // r2
  it("shows no Open conversation link for chat- or job: sessions, leaving the S3a rows unchanged", async () => {
    const items = [
      decisionItem(decisionRow("q1")), // default session_id = chat-q1
      waitingFrom("t2", "chat-abc"),
      waitingFrom("t3", "job:7"),
    ];
    const { section, cleanup } = await renderSection({ items });
    try {
      expect(findButton(section(), (t) => t === "Open conversation")).toBeUndefined();
      expect(section().querySelector(".chat-needs-you__task")).toBeNull(); // no trailing-link row layout
      // The chat-/job: task rows stay the plain single-control S3a rows.
      const li = [...section().querySelectorAll("li")].find((node) => node.textContent?.includes("Ship the release"))!;
      expect(li.querySelectorAll("button")).toHaveLength(1);
    } finally { await cleanup(); }
  });
});
