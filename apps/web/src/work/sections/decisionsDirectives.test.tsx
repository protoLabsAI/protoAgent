import { SurfaceFrameContext } from "../../shell/SurfaceFrame";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "@protolabsai/ui/overlays";

import { api } from "../../lib/api";
import { chatStore } from "../../chat/chat-store";
import { useKbIntents } from "../../keybindings/intents";
import { decisionsApi } from "../../lib/api/decisions";
import { ApiError } from "../../lib/api/http";
import { decisionsStoreForTest } from "../../decisions/useDecisions";
import { WorkView } from "../WorkView";
import { beginThisChatVisit, endThisChatVisit } from "../visitScope";
import { sections } from "../sectionRegistry";
import { instanceDirectiveStore } from "../instanceDirectives";
import { workGroups } from "../workGroups";
import { decisionMeta, fromBackgroundJob, fromDecision, fromDirective } from "../tone";
import { useUI } from "../../state/uiStore";
import type { WorkItem, WorkItems, WorkSource, WorkSourceKind } from "../useWorkItems";
import type { DecisionRow } from "../../lib/api/decisions";
import type { DirectiveRow } from "../../lib/api/chat";
import type { DirectivesSnapshot } from "../../chat/useDirectives";

// Import only WorkView: production must register its built-in sections itself.

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The Work view's data hook, driven by hand.
const data = vi.hoisted(() => ({ sources: {} as WorkItems }));
// The spend block has its own suite (SpendToday.test.tsx); a stub keeps this file's heading,
// alert and button queries on the groups it is about.
vi.mock("../SpendToday", () => ({ SpendToday: () => null }));
vi.mock("../useWorkItems", () => ({ useWorkItems: () => data.sources }));

// The directive ledger's store (the section's withdraw path), driven by hand.
const dir = vi.hoisted(() => ({
  snapshot: { state: "ready", active: [], history: [], loadedAt: 1 } as DirectivesSnapshot,
  refresh: vi.fn(),
  revoke: vi.fn(),
  sessions: new Map<string, DirectivesSnapshot>(),
}));
vi.mock("../../chat/useDirectives", () => ({
  useDirectives: (sessionId: string) => ({
    ...(dir.sessions.get(sessionId) ?? dir.snapshot), refresh: dir.refresh,
    revoke: (...args: unknown[]) => dir.revoke(...(dir.sessions.size ? [sessionId, ...args] : args)),
  }),
}));
vi.mock("../../lib/useIsMobile", () => ({ useIsMobile: () => false }));

const connection = vi.hoisted(() => ({
  online: true,
  listeners: new Set<(online: boolean) => void>(),
  topics: new Map<string, Set<(data: Record<string, unknown>) => void>>(),
}));
vi.mock("../../lib/events", () => ({
  isConnected: () => connection.online,
  onConnectionChange: (listener: (online: boolean) => void) => {
    connection.listeners.add(listener);
    listener(connection.online);
    return () => connection.listeners.delete(listener);
  },
  onTopic: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    const set = connection.topics.get(topic) ?? new Set();
    set.add(fn);
    connection.topics.set(topic, set);
    return () => set.delete(fn);
  },
}));
/** A cursor-gap resync (ADR 0130 D3): the stores re-read the server with no incremental bus
 *  event — the path that reconciles a decision settled while the stream was down. */
const fireResync = () => connection.topics.get("resync_required")?.forEach((fn) => fn({}));

const kinds: WorkSourceKind[] = ["decisions", "goals", "watches", "tasks", "schedules", "background", "activity", "directives"];
function emptySources(): WorkItems {
  return Object.fromEntries(
    kinds.map((kind) => [kind, { items: [], status: "ready", error: null, loadedAt: 1, retry: vi.fn() } as WorkSource]),
  ) as WorkItems;
}

const decisionRow = (id: string, patch: Partial<DecisionRow> = {}): DecisionRow => ({
  id,
  session_id: "here",
  agent: "protoEngineer",
  question: `Question ${id}`,
  options: [
    { id: "go", label: "Go ahead" },
    { id: "stop", label: "Stop" },
  ],
  created_at: "2026-11-08T14:45:00+00:00",
  state: "pending",
  answer: null,
  answered_at: null,
  expires_at: null,
  default_option: null,
  thread_id: `thread-${id}`,
  interrupt_id: `interrupt-${id}`,
  context: "",
  default_action: null,
  withdraw_reason: null,
  answered_by: null,
  answered_surface: null,
  default_action_text: "carry on without this step",
  answer_label: null,
  ...patch,
});
function decisionItem(row: DecisionRow): WorkItem {
  // meta mirrors the real normalizer (useWorkItems.mapDecision) so the Overview pin shows what
  // ships; the Decisions section ignores it and recomputes from `raw` with the live filter.
  return { kind: "decision", id: row.id, status: fromDecision(row.state), title: row.question, meta: decisionMeta(row, false), sessionId: row.session_id || undefined, raw: row };
}

const directiveRow = (id: string, patch: Partial<DirectiveRow> = {}): DirectiveRow => ({
  id,
  session_id: "here",
  quote: `Instruction ${id}`,
  conditions: "",
  scope: "",
  status: "in_force",
  created_at: "2026-11-07T17:14:02+00:00",
  closed_at: null,
  closed_by: null,
  close_reason: null,
  ...patch,
});
function directiveItem(row: DirectiveRow): WorkItem {
  return { kind: "directive", id: row.id, status: fromDirective(row.status), title: row.quote, meta: "", sessionId: row.session_id || undefined, raw: row };
}

let host: HTMLDivElement;
let root: Root;

async function render(frame: "rail" | "modal" | "sheet" = "rail", sessionId: string | undefined = "here") {
  await act(async () => root.render(<ToastProvider><SurfaceFrameContext.Provider value={frame}><WorkView frame={frame === "sheet" ? "modal" : frame} sessionId={sessionId} /></SurfaceFrameContext.Provider></ToastProvider>));
}
function workState(patch: Record<string, unknown>) {
  useUI.setState({ surfaceStates: { work: patch }, surfaceModal: null });
}
const rows = () => [...host.querySelectorAll<HTMLElement>(".work-row")];
const rowFor = (text: string) => rows().find((r) => r.textContent?.includes(text));
const buttons = (text: string) => [...document.querySelectorAll<HTMLButtonElement>("button")].filter((b) => b.textContent?.trim() === text);

beforeEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
  connection.online = true;
  vi.spyOn(decisionsApi, "getDecision").mockRejectedValue(new Error("unavailable"));
  // State-aware, like the real route: the shared store's refresh lists "pending"; the
  // directives section's answered-ledger top-up lists "answered".
  vi.spyOn(decisionsApi, "listDecisions").mockImplementation(async (state = "pending") => {
    const decisions = data.sources.decisions.items.map((item) => item.raw as DecisionRow).filter((row) => row.state === state);
    return { decisions, pending: decisions.filter((row) => row.state === "pending").length };
  });
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  data.sources = emptySources();
  dir.sessions.clear();
  dir.snapshot = { state: "ready", active: [], history: [], loadedAt: 1 };
  useUI.setState({ surfaceStates: {}, surfaceModal: null });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  endThisChatVisit();
});

describe("Decisions section", () => {
  it("lists only pending decisions in Needs you with 'Asked {time}' and opens the DecisionCard as its detail (r1)", async () => {
    data.sources.decisions.items = [
      decisionItem(decisionRow("closed", { state: "answered", answer_label: "Go ahead", answered_by: "operator", answered_at: "2026-11-08T15:00:00+00:00" })),
      decisionItem(decisionRow("live", { created_at: new Date(Date.now() - 4 * 60_000).toISOString() })),
    ];
    workState({ section: "decisions", filters: { thisChat: true } });
    await render("rail");

    // Finished items leave Work (v2 §1): only the pending question renders, tagged by kind.
    expect(rows()).toHaveLength(1);
    expect(rows()[0].textContent).toContain("Question live");
    expect(rows()[0].textContent).toContain("Asked 4 min ago");
    expect(rows()[0].querySelector(".pl-badge")?.textContent).toBe("Decision");
    expect(host.textContent).not.toContain("Question closed");

    // Opening it pushes the unchanged DecisionCard into the detail pane.
    workState({ section: "decisions", itemId: "decision:live", filters: { thisChat: true } });
    await render("rail");
    const detail = host.querySelector(".work-view__detail");
    expect(detail?.textContent).toContain("protoEngineer");
    expect(detail?.textContent).toContain("Go ahead");
    expect(detail?.textContent).toContain("Reply in chat instead"); // a DecisionCard-only affordance
    expect(detail?.querySelector('[role="group"][aria-label="Answer"]')).toBeTruthy();
    expect(detail?.querySelector(".pl-card")).toBeNull(); // the pane is the container (#5668 §1)
  });

  it("pins a pending decision first in the Overview Needs you lane with 'Asked {time}', opening the DecisionCard (r1 §2.1)", async () => {
    data.sources.decisions.items = [decisionItem(decisionRow("live", { created_at: new Date(Date.now() - 4 * 60_000).toISOString() }))];
    // A running task fills the Doing now group; the pending decision must still sit above it (§1).
    data.sources.tasks.items = [{ kind: "task", id: "t1", status: "running", title: "Build the thing", meta: "", raw: { status: "in_progress" } }];
    workState({ section: "overview", filters: { thisChat: false } });
    await render("modal");

    const lanes = [...host.querySelectorAll("section")];
    // "Needs you" is the first group, and its first (pinned) row is the pending decision with
    // the §2.4 field-3 pin — "Asked {time}".
    expect(lanes[0].querySelector("h2")?.firstChild?.textContent).toBe("Needs you");
    const firstRow = lanes[0].querySelector(".work-row");
    expect(firstRow?.textContent).toContain("Question live");
    expect(firstRow?.textContent).toContain("Asked 4 min ago");
    expect(lanes[1].querySelector("h2")?.firstChild?.textContent).toBe("Doing now"); // sits below Needs you

    // Opening the pin straight from the Overview renders the unchanged DecisionCard as its detail.
    await act(async () => (firstRow as HTMLButtonElement).click());
    expect(useUI.getState().surfaceStates.work.itemId).toBe("decision:live");
    const detail = host.querySelector(".work-view__detail");
    expect(detail?.textContent).toContain("Question live");
    expect(detail?.textContent).toContain("Reply in chat instead"); // a DecisionCard-only affordance
    expect(detail?.querySelector('[role="group"][aria-label="Answer"]')).toBeTruthy();
  });

  it("closed decisions are history and leave Work entirely (v2 §1, ADR Q3)", async () => {
    data.sources.decisions.items = [
      decisionItem(decisionRow("ans", { state: "answered", answer_label: "Merge it", answered_by: "operator", answered_at: "2026-11-08T15:00:00+00:00" })),
      decisionItem(decisionRow("exp", { state: "expired" })),
      decisionItem(decisionRow("wd", { state: "withdrawn", withdraw_reason: "No longer needed" })),
      decisionItem(decisionRow("can", { state: "cancelled" })),
      decisionItem(decisionRow("live")),
    ];
    workState({ section: "decisions", filters: { thisChat: true } });
    await render("rail");

    expect(rows()).toHaveLength(1);
    expect(rows()[0].textContent).toContain("Question live");
    for (const gone of ["Question ans", "Question exp", "Question wd", "Question can"]) {
      expect(host.textContent).not.toContain(gone);
    }
  });

  it("names the agent on every decision row, since Work always lists every chat (r1 §2.4 field 5)", async () => {
    data.sources.decisions.items = [decisionItem(decisionRow("p", { session_id: "here" }))];
    for (const frame of ["rail", "modal"] as const) {
      workState({ section: "decisions" });
      await render(frame);
      expect(host.querySelector(".work-row")?.textContent).toContain("protoEngineer");
    }
  });

  it("keeps decision, thread and interrupt ids out of the row and the card (r4)", async () => {
    data.sources.decisions.items = [
      decisionItem(decisionRow("d-id-777", { question: "Should we ship?", thread_id: "thr-999", interrupt_id: "int-888", session_id: "here" })),
    ];

    workState({ section: "decisions", filters: { thisChat: true } });
    await render("rail");
    expect(host.textContent).toContain("Should we ship?");
    for (const id of ["d-id-777", "thr-999", "int-888"]) expect(host.textContent).not.toContain(id);

    workState({ section: "decisions", itemId: "decision:d-id-777", filters: { thisChat: true } });
    await render("rail");
    expect(host.querySelector(".work-view__detail")?.textContent).toContain("Should we ship?");
    for (const id of ["d-id-777", "thr-999", "int-888"]) expect(host.textContent).not.toContain(id);
  });
});

describe("Work decision actions", () => {
  it.each([false, true])("opens a different source session, reply=%s, retaining Work state", async (reply) => {
    const source = chatStore.createSession({ incognito: true });
    const current = chatStore.createSession();
    chatStore.switchSession(current.id);
    data.sources.decisions.items = [decisionItem(decisionRow("navigate", { session_id: source.id }))];
    const saved = { section: "decisions", itemId: "decision:navigate", filters: { thisChat: false }, scrollTop: 81 };
    workState(saved);
    useUI.getState().openSurfaceModal("work");
    useUI.setState({ surface: "knowledge", mobileActive: "work", leftCollapsed: true });
    const nonce = useKbIntents.getState().composerFocusNonce;
    await render("modal", current.id);
    const card = host.querySelector(".work-decision-detail")!;
    const action = reply ? buttons("Reply in chat instead")[0] : card.querySelector<HTMLAnchorElement>("a")!;
    await act(async () => action.click());
    expect(chatStore.getSnapshot().currentSessionId).toBe(source.id);
    expect(useUI.getState()).toMatchObject({ surface: "chat", mobileActive: "chat", leftCollapsed: false, surfaceModal: null });
    expect(useKbIntents.getState().composerFocusNonce).toBe(nonce + Number(reply));
    expect(useUI.getState().surfaceStates.work).toEqual(saved);
    await render("rail", source.id);
    expect(host.querySelector(".work-decision-detail")).toBe(card);
    expect(host.querySelector(".work-view__list")?.scrollTop).toBe(81);
    expect(useUI.getState().surfaceStates.work.filters).toEqual({ thisChat: false });
  });

  it.each([false, true])("loads a durable source before navigation, failure=%s", async (fails) => {
    const sourceId = fails ? "missing-source" : "durable-source";
    const current = chatStore.createSession();
    const load = vi.spyOn(api, "chatSessionTurns");
    if (fails) load.mockRejectedValue(new Error("offline"));
    else load.mockResolvedValue({ turns: [{
      task_id: "durable-task", state: "TASK_STATE_COMPLETED", last_updated: "2026-11-08T14:45:00Z", text: "Demo response",
      artifacts: [{ parts: [{ text: "Demo response" }] }],
      history: [{ role: "ROLE_USER", parts: [{ text: "Demo question" }] }],
    }] });
    data.sources.decisions.items = [decisionItem(decisionRow(sourceId, { session_id: sourceId }))];
    workState({ section: "decisions", itemId: `decision:${sourceId}`, filters: { thisChat: false } });
    await render("modal", current.id);
    const nonce = useKbIntents.getState().composerFocusNonce;
    await act(async () => buttons("Reply in chat instead")[0].click());
    expect(load).toHaveBeenCalledExactlyOnceWith(sourceId, 50);
    expect(chatStore.getSnapshot().currentSessionId).toBe(fails ? current.id : sourceId);
    expect(useKbIntents.getState().composerFocusNonce).toBe(nonce + Number(!fails));
    if (fails) expect(document.body.textContent).toContain("This chat isn't available");
    else expect(chatStore.getSnapshot().sessions.find((session) => session.id === sourceId)?.messages.some((message) => message.content === "Demo response")).toBe(true);
  });

  it("tracks disconnect and reconnect without remounting the detail", async () => {
    data.sources.decisions.items = [decisionItem(decisionRow("presence"))];
    workState({ section: "decisions", itemId: "decision:presence", filters: { thisChat: true } });
    await render();
    const detail = host.querySelector(".work-decision-detail");
    expect(detail?.textContent).not.toContain("offline");
    await act(async () => {
      connection.online = false;
      connection.listeners.forEach((listener) => listener(false));
    });
    expect(detail?.textContent).toContain("offline");
    await act(async () => {
      connection.online = true;
      connection.listeners.forEach((listener) => listener(true));
    });
    expect(host.querySelector(".work-decision-detail")).toBe(detail);
    expect(detail?.textContent).not.toContain("offline");
  });

  it.each(["answered", "expired", "withdrawn", "cancelled"] as const)("reconciles a selected %s decision after reconnect without a bus event", async (state) => {
    const id = `reconnect-${state}`;
    const pending = decisionRow(id);
    const terminal = decisionRow(id, { state, answer_label: state === "answered" ? "Stop" : null });
    data.sources.decisions.items = [decisionItem(pending)];
    const saved = { section: "decisions", itemId: `decision:${id}`, filters: { thisChat: true }, scrollTop: 81 };
    workState(saved);
    await render();
    const detail = host.querySelector(".work-decision-detail")!;
    expect(detail.querySelector('[role="group"][aria-label="Answer"]')).toBeTruthy();
    await act(async () => {
      connection.online = false;
      connection.listeners.forEach((listener) => listener(false));
    });
    const get = vi.mocked(decisionsApi.getDecision).mockImplementation(async (requested) => {
      if (requested === id) return terminal;
      throw new Error("unavailable");
    });
    vi.mocked(decisionsApi.listDecisions).mockResolvedValue({ decisions: [], pending: 0 });
    await act(async () => {
      connection.online = true;
      connection.listeners.forEach((listener) => listener(true));
    });
    expect(get.mock.calls.filter(([requested]) => requested === id)).toEqual([[id]]);
    expect(decisionsStoreForTest.getSnapshot().records[id]).toEqual(terminal);
    expect(host.querySelector(".work-decision-detail")).toBe(detail);
    expect(detail.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
    expect(detail.textContent).toContain(state === "answered" ? "Stop" : state === "expired" ? "No answer by" : "doesn't need this any more");
    expect(useUI.getState().surfaceStates.work).toEqual(saved);
    expect(host.querySelector(".work-view__list")?.scrollTop).toBe(81);
  });

  it("reconciles a selected decision on resync_required with the stream still up (no connection edge)", async () => {
    // The cursor-gap case: a `resync_required` arrives on an OPEN stream (no offline→online
    // edge), and the store must still re-read and reconcile the settled decision (ADR 0130 D3).
    const id = "resync-answered";
    const terminal = decisionRow(id, { state: "answered", answer_label: "Stop" });
    data.sources.decisions.items = [decisionItem(decisionRow(id))];
    workState({ section: "decisions", itemId: `decision:${id}`, filters: { thisChat: true } });
    await render();
    const detail = host.querySelector(".work-decision-detail")!;
    expect(detail.querySelector('[role="group"][aria-label="Answer"]')).toBeTruthy();
    const get = vi.mocked(decisionsApi.getDecision).mockImplementation(async (requested) => {
      if (requested === id) return terminal;
      throw new Error("unavailable");
    });
    vi.mocked(decisionsApi.listDecisions).mockResolvedValue({ decisions: [], pending: 0 });
    await act(async () => fireResync());
    expect(get.mock.calls.filter(([requested]) => requested === id)).toEqual([[id]]);
    expect(decisionsStoreForTest.getSnapshot().records[id]).toEqual(terminal);
    expect(host.querySelector(".work-decision-detail")).toBe(detail);
    expect(detail.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
    expect(detail.textContent).toContain("Stop");
  });

  it("drops a selected decision the server reports gone (404) after reconnect", async () => {
    const id = "reconnect-gone";
    data.sources.decisions.items = [decisionItem(decisionRow(id))];
    const saved = { section: "decisions", itemId: `decision:${id}`, filters: { thisChat: true }, scrollTop: 81 };
    workState(saved);
    await render();
    expect(host.querySelector('[role="group"][aria-label="Answer"]')).toBeTruthy();
    await act(async () => {
      connection.online = false;
      connection.listeners.forEach((listener) => listener(false));
    });
    vi.mocked(decisionsApi.listDecisions).mockResolvedValue({ decisions: [], pending: 0 });
    vi.mocked(decisionsApi.getDecision).mockRejectedValue(new ApiError(404, "gone"));
    await act(async () => {
      connection.online = true;
      connection.listeners.forEach((listener) => listener(true));
    });
    expect(decisionsStoreForTest.getSnapshot().missing[id]).toBe(true);
    expect(host.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
    expect(rowFor(`Question ${id}`)).toBeUndefined();
    expect(useUI.getState().surfaceStates.work).toEqual(saved);
  });

  it("a settled Work row wins over a shared record still cached as pending (the reconcile GET failed)", async () => {
    const id = "reconnect-503";
    const pending = decisionRow(id);
    data.sources.decisions.items = [decisionItem(pending)];
    workState({ section: "decisions", itemId: `decision:${id}`, filters: { thisChat: true } });
    await render();
    expect(decisionsStoreForTest.getSnapshot().records[id]?.state).toBe("pending");
    await act(async () => {
      connection.online = false;
      connection.listeners.forEach((listener) => listener(false));
    });
    vi.mocked(decisionsApi.listDecisions).mockResolvedValue({ decisions: [], pending: 0 });
    vi.mocked(decisionsApi.getDecision).mockRejectedValue(new ApiError(503, "unavailable"));
    // Work's own list re-reads on reconnect (useWorkItems) and now carries the settled row.
    data.sources.decisions.items = [decisionItem(decisionRow(id, { state: "answered", answer_label: "Stop" }))];
    await act(async () => {
      connection.online = true;
      connection.listeners.forEach((listener) => listener(true));
    });
    expect(decisionsStoreForTest.getSnapshot().records[id].state).toBe("pending");
    expect(host.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
    expect(host.querySelector(".work-decision-detail")?.textContent).toContain("Stop");
  });

  it("keeps a new pending decision from Work's list when the shared refresh fails", async () => {
    data.sources.decisions.items = [];
    workState({ section: "decisions", filters: { thisChat: true } });
    await render();
    expect(decisionsStoreForTest.getSnapshot().loaded).toBe(true);
    vi.mocked(decisionsApi.listDecisions).mockRejectedValue(new Error("down"));
    data.sources.decisions.items = [decisionItem(decisionRow("brand-new"))];
    await render();
    expect(rowFor("Question brand-new")).toBeTruthy();
  });

  it.each(["answered", "expired", "withdrawn", "cancelled"] as const)("shares %s conflicts and retains the selected terminal record", async (state) => {
    const id = `conflict-${state}`;
    const pending = decisionRow(id);
    const terminal = decisionRow(id, { state, answer_label: state === "answered" ? "Stop" : null });
    data.sources.decisions.items = [decisionItem(pending)];
    const answer = vi.spyOn(decisionsApi, "answerDecision").mockImplementation(async () => {
      data.sources.decisions.items = [];
      return { answered_elsewhere: true, decision: terminal };
    });
    workState({ section: "decisions", itemId: `decision:${id}`, filters: { thisChat: true } });
    await render();
    await act(async () => buttons("Go ahead")[0].click());
    expect(answer).toHaveBeenCalledExactlyOnceWith(id, { option_id: "go", surface: "console" });
    expect(decisionsStoreForTest.getSnapshot().records[id]).toEqual(terminal);
    const detail = host.querySelector(".work-decision-detail")!;
    expect(detail.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
    expect(detail.textContent?.includes("Already answered")).toBe(state === "answered");
    expect(detail.contains(document.activeElement)).toBe(true);
    expect(useUI.getState().surfaceStates.work.itemId).toBe(`decision:${id}`);
  });

  it("updates an idle detail from the same shared cache when another surface answers", async () => {
    const pending = decisionRow("cache-update");
    data.sources.decisions.items = [decisionItem(pending)];
    workState({ section: "decisions", itemId: "decision:cache-update", filters: { thisChat: true } });
    await render();
    const terminal = { ...pending, state: "withdrawn" as const, withdraw_reason: "No longer required" };
    vi.spyOn(decisionsApi, "answerDecision").mockImplementation(async () => {
      data.sources.decisions.items = [];
      return { answered_elsewhere: true, decision: terminal };
    });
    await act(async () => { await decisionsStoreForTest.answer(pending.id, "go"); });
    const detail = host.querySelector(".work-decision-detail")!;
    expect(detail.textContent).toContain("No longer required");
    expect(detail.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
  });
});

describe("Directives section (Your instructions, ADR 0129 §3)", () => {
  // Seed the shared decisions store so linkDecision can resolve an ANSWERED decision by id — the
  // record a decision leaves there the moment it settles and drops off the pending list (§3.1).
  async function seedAnswered(row: DecisionRow) {
    vi.mocked(decisionsApi.getDecision).mockResolvedValueOnce(row);
    await act(async () => { await decisionsStoreForTest.recheck(row.id); });
  }

  it("renders an in-force directive as an Instruction: scope headline, muted 'You said' provenance (r1)", async () => {
    const quote = "Don't merge anything into protoAgent main until I say so.";
    data.sources.directives.items = [directiveItem(directiveRow("i1",
      { session_id: "here", quote, scope: "Hold protoAgent main", created_at: new Date().toISOString() }))];
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail", "here");

    const row = rowFor("Hold protoAgent main")!;
    expect(row.querySelector(".work-row__title")?.textContent).toBe("Hold protoAgent main"); // the scope, not the quote
    expect(row.querySelector(".pl-badge")?.textContent).toBe("Instruction");
    expect(row.textContent).toContain(`You said "${quote}" in this chat`);
    expect(row.textContent).toMatch(/today \d{2}:\d{2}/);
  });

  it("falls back to the quote when scope is empty and never shows an id (r1, r4)", async () => {
    data.sources.directives.items = [directiveItem(directiveRow("secret-dir-id",
      { session_id: "here", quote: "Approve dependency bumps", scope: "" }))];
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail", "here");
    expect(rowFor("Approve dependency bumps")?.querySelector(".work-row__title")?.textContent).toBe("Approve dependency bumps");
    expect(host.textContent).not.toContain("secret-dir-id");
  });

  it("shows only in-force entries — fulfilled and revoked history never appears (r1)", async () => {
    data.sources.directives.items = [
      directiveItem(directiveRow("live", { session_id: "here", quote: "Keep the staging deploy paused" })),
      directiveItem(directiveRow("done", { session_id: "here", quote: "Cut the patch release", status: "fulfilled", closed_at: new Date().toISOString() })),
      directiveItem(directiveRow("gone", { session_id: "here", quote: "Old instruction", status: "revoked", closed_at: new Date().toISOString() })),
    ];
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail", "here");
    expect(rows()).toHaveLength(1);
    expect(host.textContent).toContain("Keep the staging deploy paused");
    expect(host.textContent).not.toContain("Cut the patch release");
    expect(host.textContent).not.toContain("Old instruction");
  });

  it("tags a decision-linked directive Approval and shows the option, question and Open decision (r1 §3.1)", async () => {
    const now = Date.now();
    await seedAnswered(decisionRow("appr-dec", {
      session_id: "inst-chat", question: "Can I approve the dependency bumps?", state: "answered",
      answer: "go", answer_label: "Go ahead", answered_by: "operator", answered_at: new Date(now - 60_000).toISOString(),
    }));
    data.sources.directives.items = [directiveItem(directiveRow("appr",
      { session_id: "inst-chat", quote: "Dependency bumps are fine", scope: "Approve dependency bumps", created_at: new Date(now).toISOString() }))];

    workState({ section: "directives", filters: { thisChat: false } });
    await render("rail", "inst-chat");
    const row = rowFor("Approve dependency bumps")!;
    expect(row.querySelector(".pl-badge")?.textContent).toBe("Approval");
    expect(row.textContent).toContain('You chose "Go ahead"');
    expect(row.textContent).toMatch(/\d{2}:\d{2}/);

    // The detail carries the full approval provenance, with Open decision.
    workState({ section: "directives", itemId: "directive:appr", filters: { thisChat: false } });
    await render("rail", "inst-chat");
    const detail = host.querySelector(".work-view__detail")!;
    expect(detail.textContent).toContain("You were asked");
    expect(detail.textContent).toContain("Can I approve the dependency bumps?");
    expect(detail.textContent).toContain("You chose");
    expect(detail.textContent).toContain("Go ahead");
    expect(detail.textContent).toContain("Your words");
    expect(detail.textContent).toContain('You said "Dependency bumps are fine" in this chat');
    expect(buttons("Open decision")).toHaveLength(1);

    // Open decision opens the linked decision IN PLACE (the Work detail, via the retained
    // settled record) — it does not switch to the source chat or close the surface.
    await act(async () => buttons("Open decision")[0].click());
    expect(useUI.getState().surfaceStates.work.itemId).toBe("decision:appr-dec");
    const decisionDetail = host.querySelector(".work-view__detail")!;
    expect(decisionDetail.textContent).toContain("Can I approve the dependency bumps?");
    expect(decisionDetail.textContent).toContain("Go ahead");
  });

  it("links an Approval from the answered ledger when the answer was never seen pending here (reload, r1 §3.1)", async () => {
    const now = Date.now();
    // The answered record exists ONLY on the server (a reload: the shared store's record
    // cache starts empty, and the pending refresh never lists answered rows).
    const answered = decisionRow("reload-dec", {
      session_id: "reload-chat", question: "Ship the hotfix?", state: "answered",
      answer: "go", answer_label: "Go ahead", answered_by: "operator", answered_at: new Date(now - 60_000).toISOString(),
    });
    vi.mocked(decisionsApi.listDecisions).mockImplementation(async (state = "pending") =>
      state === "answered" ? { decisions: [answered], pending: 0 } : { decisions: [], pending: 0 });
    data.sources.directives.items = [directiveItem(directiveRow("reload-dir", {
      session_id: "reload-chat", quote: "Hotfix away", scope: "Ship the hotfix", created_at: new Date(now).toISOString(),
    }))];
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail", "reload-chat");
    const row = rowFor("Ship the hotfix")!;
    expect(row.querySelector(".pl-badge")?.textContent).toBe("Approval");
    expect(row.textContent).toContain('You chose "Go ahead"');
  });

  it.each(["directive:other", "other"])("opens the selected owner's instruction and withdraws it via the existing confirm path (%s)", async (itemId) => {
    const here = directiveRow("focused", { session_id: "here", quote: "Focused chat instruction" });
    const other = directiveRow("other", { session_id: "elsewhere", quote: "Other chat instruction" });
    data.sources.directives.items = [directiveItem(here), directiveItem(other)];
    // Non-empty sessions make the revoke mock prepend the owning session id, as production does.
    dir.sessions.set("here", { state: "ready", active: [here], history: [], loadedAt: 1 });
    dir.sessions.set("elsewhere", { state: "ready", active: [other], history: [], loadedAt: 1 });
    workState({ section: "overview", itemId, filters: { thisChat: false } });
    await render("rail", "here");
    const detail = host.querySelector(".work-view__detail")!;
    expect(detail.textContent).toContain("Other chat instruction");
    expect(detail.textContent).not.toContain("Focused chat instruction");
    dir.revoke.mockResolvedValue({ ok: true });
    const withdraw = (label: string) => [...detail.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.trim() === label)!;
    await act(async () => withdraw("Withdraw…").click());
    await act(async () => withdraw("Withdraw").click());
    expect(dir.revoke).toHaveBeenCalledWith("elsewhere", "other", "");
  });

  it("shows its own empty state when nothing is in force, even with no session in view", async () => {
    data.sources.directives.items = [];
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail", undefined);
    expect(host.textContent).toContain("No standing instructions");
  });

  it("renders an explicit tied job and drops it — with its count — the instant the job ends (r1 §3, Q2)", async () => {
    const tied = directiveRow("tied", { session_id: "here", quote: "Keep protoAgent main frozen", scope: "Freeze protoAgent main" });
    const withJob = (status: string) => {
      data.sources.directives.items = [directiveItem(tied)];
      // The job link is an EXPLICIT projection field (DirectiveRow carries none); never parsed.
      data.sources.directives.groupItems = [{ ...directiveItem(tied), tiedJob: { kind: "background", id: "deploy-job" } }];
      data.sources.background.items = [{ kind: "background", id: "deploy-job", status: fromBackgroundJob(status), title: "Nightly deploy", meta: "", raw: { status } }];
    };

    // Running: the instruction shows, its meta ends with the job, and the count is 1.
    withJob("running");
    workState({ section: "directives", itemId: "directive:tied", filters: { thisChat: true } });
    await render("rail", "here");
    expect(rows()).toHaveLength(1);
    expect(host.querySelector(".work-heading")?.getAttribute("aria-label")).toBe("Your instructions, 1");
    expect(rowFor("Freeze protoAgent main")?.textContent).toContain("ends with Nightly deploy");
    const detail = host.querySelector(".work-view__detail")!;
    expect(detail.textContent).toContain('until "Nightly deploy" finishes. Then it closes itself.');
    expect(buttons("Open the job")).toHaveLength(1);

    // Open the job opens the tied job's own Work detail in place.
    await act(async () => buttons("Open the job")[0].click());
    expect(useUI.getState().surfaceStates.work.itemId).toBe("background:deploy-job");
    expect(host.querySelector(".work-view__detail")?.textContent).toContain("Nightly deploy");
    // Restore the instruction selection for the ended-job phase below.
    workState({ section: "directives", itemId: "directive:tied", filters: { thisChat: true } });

    // Ended: the shared projection drops the row AND the count together — no phantom instruction.
    withJob("completed");
    await render("rail", "here");
    expect(rows()).toHaveLength(0);
    expect(host.querySelector(".work-heading")?.getAttribute("aria-label")).toBe("Your instructions, 0");
    expect(host.textContent).not.toContain("Freeze protoAgent main");
  });

  it("shows in-force directives under Your instructions and drops closed ones from Work (v2 §1)", async () => {
    data.sources.directives.items = [
      directiveItem(directiveRow("inforce")),
      directiveItem(directiveRow("fulfilled", { status: "fulfilled", closed_at: new Date().toISOString() })),
      directiveItem(directiveRow("revoked", { status: "revoked", closed_at: new Date().toISOString() })),
    ];
    workState({ section: "overview", filters: { thisChat: false } });
    await render("modal");
    const lanes = [...host.querySelectorAll("section h2")].map((n) => n.firstChild?.textContent);
    expect(lanes).toEqual(["Your instructions"]);
    expect(host.textContent).toContain("Instruction inforce");
    expect(host.textContent).not.toContain("Instruction fulfilled");
    expect(host.textContent).not.toContain("Instruction revoked");
  });

  it("shows a skeleton while loading and a Retry alert when loading failed — never 'No standing instructions' (r1)", async () => {
    data.sources.directives = { ...data.sources.directives, items: [], status: "loading", loadedAt: null };
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail", "here");
    expect(host.querySelector('[aria-label="Loading your instructions"]')).toBeTruthy();
    expect(host.textContent).not.toContain("No standing instructions");

    data.sources.directives = { ...data.sources.directives, status: "error", error: new Error("down") };
    await render("rail", "here");
    expect(host.textContent).toContain("Couldn't load standing instructions. The agent still has them.");
    expect(host.textContent).not.toContain("No standing instructions");
    await act(async () => buttons("Retry")[0].click());
    expect(data.sources.directives.retry).toHaveBeenCalled();
  });

  it("keeps an open detail honest while the source hasn't loaded — no 'no longer here' claim (r1)", async () => {
    data.sources.directives = { ...data.sources.directives, items: [], status: "loading", loadedAt: null };
    workState({ section: "directives", itemId: "directive:unseen", filters: { thisChat: true } });
    await render("rail", "here");
    expect(host.querySelector(".work-view__detail [aria-label='Loading your instructions']")).toBeTruthy();
    expect(host.textContent).not.toContain("This instruction is no longer here.");

    data.sources.directives = { ...data.sources.directives, status: "error", error: new Error("down") };
    await render("rail", "here");
    expect(host.querySelector(".work-view__detail")?.textContent).toContain("Couldn't load standing instructions");
    expect(host.textContent).not.toContain("This instruction is no longer here.");
  });

  it("keeps the retained rows under a stale callout with Retry when a refresh failed (r1)", async () => {
    data.sources.directives = {
      ...data.sources.directives, status: "stale", loadedAt: 5,
      items: [directiveItem(directiveRow("kept", { session_id: "here", quote: "Keep the deploy paused" }))],
    };
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail", "here");
    expect(host.textContent).toContain("Showing the last load. Couldn't refresh.");
    expect(rowFor("Keep the deploy paused")).toBeTruthy();
    await act(async () => buttons("Retry")[0].click());
    expect(data.sources.directives.retry).toHaveBeenCalled();
  });

  it("resolves a closed directive deep link read-only — terminal history has no Overview row (v2 §1)", async () => {
    data.sources.directives.items = [directiveItem(directiveRow("done-dir", {
      session_id: "here", quote: "Cut the patch release", status: "fulfilled",
      closed_at: new Date().toISOString(), closed_by: "agent",
    }))];
    workState({ section: "overview", filters: { thisChat: false } });
    await render("modal", "here");
    // Finished items leave Work entirely — no Done lane, no row (ADR Q3).
    expect(rowFor("Cut the patch release")).toBeUndefined();
    // A retained selection (the row closed while open, or a saved deep link) still resolves
    // to the read-only detail with its closing sentence.
    workState({ section: "overview", itemId: "directive:done-dir", filters: { thisChat: false } });
    await render("modal", "here");
    const detail = host.querySelector(".work-view__detail")!;
    expect(detail.textContent).toContain("Cut the patch release");
    expect(detail.textContent).toContain("the agent marked it complete"); // closingSentence
    expect(buttons("Withdraw…")).toHaveLength(0); // closed rows are read-only
  });

  it("a rail deep link's visit scope filters the rows for the visit without touching the saved filters (S6a)", async () => {
    data.sources.directives.items = [
      directiveItem(directiveRow("mine", { session_id: "here", quote: "Keep replies short" })),
      directiveItem(directiveRow("theirs", { session_id: "elsewhere", quote: "Other chat instruction" })),
    ];
    workState({ section: "directives", filters: { retained: true } });
    beginThisChatVisit();
    await render("rail", "here");
    // Scoped rows for THIS visit; Work's saved filters are never rewritten (Work itself has
    // no scope toggle, so a persisted flag would be irreversible).
    expect(rowFor("Keep replies short")).toBeTruthy();
    expect(rowFor("Other chat instruction")).toBeUndefined();
    expect(host.querySelector(".work-heading")?.getAttribute("aria-label")).toBe("Your instructions, 1");
    expect(useUI.getState().surfaceStates.work.filters).toEqual({ retained: true });
    // The visit ends: the SAME saved state lists every chat's instructions again.
    await act(async () => endThisChatVisit());
    expect(rowFor("Other chat instruction")).toBeTruthy();
    expect(host.querySelector(".work-heading")?.getAttribute("aria-label")).toBe("Your instructions, 2");
    expect(useUI.getState().surfaceStates.work.filters).toEqual({ retained: true });
  });

  it("the visit scope never overrides a saved thisChat filter a test (or future toggle) set itself", async () => {
    data.sources.directives.items = [directiveItem(directiveRow("mine", { session_id: "here", quote: "Keep replies short" }))];
    workState({ section: "directives", filters: { thisChat: true } });
    beginThisChatVisit();
    await render("rail", "here");
    expect(rowFor("Keep replies short")).toBeTruthy();
    // Ending the visit leaves the saved filter in charge — the overlay only ever ADDS scope.
    await act(async () => endThisChatVisit());
    expect(rowFor("Keep replies short")).toBeTruthy();
    expect(useUI.getState().surfaceStates.work.filters).toEqual({ thisChat: true });
  });

  it("opens another chat's in-force instruction from the Overview even under the this-chat filter (r1)", async () => {
    data.sources.directives.items = [
      directiveItem(directiveRow("mine", { session_id: "here", quote: "Focused chat instruction" })),
      directiveItem(directiveRow("theirs", { session_id: "elsewhere", quote: "Other chat instruction" })),
    ];
    workState({ section: "overview", itemId: "directive:theirs", filters: { thisChat: true } });
    await render("rail", "here");
    const detail = host.querySelector(".work-view__detail")!;
    expect(detail.textContent).toContain("Other chat instruction");
    expect(detail.textContent).not.toContain("This instruction is no longer here.");
    expect(buttons("Withdraw…")).toHaveLength(1); // still in force, so still withdrawable
  });

  it("keeps the already-closed notice in place when a withdrawal conflicts (r1)", async () => {
    const row = directiveRow("conf", { session_id: "here", quote: "Hold the release" });
    data.sources.directives.items = [directiveItem(row)];
    dir.revoke.mockResolvedValue({ conflict: {
      ...row, status: "fulfilled", closed_at: "2026-11-08T15:00:00+00:00", closed_by: "operator",
    } });
    workState({ section: "directives", itemId: "directive:conf", filters: { thisChat: true } });
    await render("rail", "here");
    await act(async () => buttons("Withdraw…")[0].click());
    await act(async () => buttons("Withdraw")[0].click());
    const detail = host.querySelector(".work-view__detail")!;
    expect(detail.textContent).toContain("This was already closed:");
    expect(detail.textContent).toContain("Nothing changed.");
  });

  it("toasts when the instruction is already gone (404), since the row's detail unmounts with it", async () => {
    data.sources.directives.items = [directiveItem(directiveRow("vanished", { session_id: "here", quote: "Old words" }))];
    dir.revoke.mockResolvedValue({ gone: true });
    workState({ section: "directives", itemId: "directive:vanished", filters: { thisChat: true } });
    await render("rail", "here");
    await act(async () => buttons("Withdraw…")[0].click());
    await act(async () => buttons("Withdraw")[0].click());
    expect(document.querySelector(".pl-toast__msg")?.textContent).toContain("This instruction is no longer in the list.");
  });
});

describe("Instance-wide standing orders (work-view-additions §3, §4)", () => {
  const wideRow = (id: string, patch: Partial<DirectiveRow> = {}): DirectiveRow => directiveRow(id, {
    session_id: "elsewhere", quote: `Order ${id}`, instance_wide: true, author: "operator", links: [], ...patch,
  });
  const closedRow = (row: DirectiveRow): DirectiveRow =>
    ({ ...row, status: "revoked", closed_by: "operator", closed_at: new Date().toISOString(), close_reason: "revoked by the operator" });
  const setItems = (rows: DirectiveRow[]) => {
    data.sources = { ...data.sources, directives: { ...data.sources.directives, items: rows.map(directiveItem) } };
  };
  const heading = () => host.querySelector<HTMLElement>(".work-view__list h2.work-heading");
  const detail = () => host.querySelector<HTMLElement>(".work-view__detail");
  const withdrawnRegion = () => host.querySelector<HTMLElement>("[data-withdrawn-announcement]")!;
  const listRegion = () => host.querySelector<HTMLElement>(".work-view__announcement[role=\"status\"]")!;
  const type = (el: HTMLTextAreaElement, value: string) => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  };
  let withdrawApi: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    instanceDirectiveStore.resetForTest();
    vi.spyOn(api, "listInstanceDirectives").mockImplementation(async () => {
      const directives = data.sources.directives.items.map((item) => item.raw as DirectiveRow).filter((row) => row.instance_wide);
      return { directives, in_force: directives.filter((row) => row.status === "in_force").length };
    });
    // Like the real route: the order closes on the server, and the (mocked) Work source lists
    // it closed from then on.
    withdrawApi = vi.spyOn(api, "withdrawInstanceDirective").mockImplementation(async (id: string) => {
      const rows = data.sources.directives.items.map((item) => item.raw as DirectiveRow);
      const directive = closedRow(rows.find((row) => row.id === id)!);
      setItems(rows.map((row) => (row.id === id ? directive : row)));
      return { directive };
    });
  });

  async function openConfirm(id: string) {
    workState({ section: "instructions", itemId: `directive:${id}`, filters: { thisChat: true } });
    await render("rail", "here");
    await act(async () => buttons("Withdraw…")[0].click());
  }

  it("lists an order recorded in another chat first, with Instance-wide then Instruction and the reach leading the meta (r1, r2)", async () => {
    setItems([
      directiveRow("mine", { session_id: "here", quote: "Chat instruction" }),
      directiveRow("theirs", { session_id: "elsewhere", quote: "Another chat's instruction" }),
      wideRow("dir-i1", { quote: "Always summarise before you stop", created_at: new Date().toISOString() }),
    ]);
    workState({ section: "instructions", filters: { thisChat: true } });
    await render("rail", "here");

    // Recorded in "elsewhere", yet listed in this chat — before the chat's own instruction.
    expect(rows().map((row) => row.querySelector(".work-row__title")?.textContent)).toEqual(["Always summarise before you stop", "Chat instruction"]);
    const row = rows()[0];
    expect([...row.querySelectorAll(".pl-badge")].map((badge) => badge.textContent)).toEqual(["Instance-wide", "Instruction"]);
    expect([...row.querySelectorAll(".pl-badge")].every((badge) => badge.className.includes("neutral") || !/info|warning|error|success/.test(badge.className))).toBe(true);
    expect(row.querySelector(".work-row__meta")?.textContent).toMatch(
      /^Applies to every chat and unattended turn · You said “Always summarise before you stop” · today \d{2}:\d{2}$/);
    // The chat's own row keeps its single badge.
    expect([...rows()[1].querySelectorAll(".pl-badge")].map((badge) => badge.textContent)).toEqual(["Instruction"]);

    // Work's count and the rail's chip source read the same selector, so they agree — in
    // this chat and in one that never recorded anything.
    expect(heading()?.getAttribute("aria-label")).toBe("Your instructions, 2");
    expect(workGroups(data.sources, { thisChatOnly: true, sessionId: "here" })[3].count).toBe(2);
    expect(workGroups(data.sources, { thisChatOnly: true, sessionId: "brand-new" })[3].count).toBe(1);

    // The Overview draws the same two badges.
    workState({ section: "overview", filters: { thisChat: true } });
    await render("rail", "here");
    expect([...rowFor("Always summarise")!.querySelectorAll(".pl-badge")].map((badge) => badge.textContent)).toEqual(["Instance-wide", "Instruction"]);
  });

  it("falls back to the dir-i id prefix only when the wire field is missing (§3.1)", async () => {
    setItems([
      wideRow("dir-i7", { instance_wide: undefined, quote: "Old server row" }),
      wideRow("dir-i8", { instance_wide: false, quote: "Field says no", session_id: "here" }),
    ]);
    workState({ section: "instructions", filters: { thisChat: true } });
    await render("rail", "here");
    expect(rowFor("Old server row")!.textContent).toContain("Instance-wide");
    expect(rowFor("Field says no")!.textContent).not.toContain("Instance-wide");
  });

  it("shows the detail fields, actions and Technical details, with focus left on the heading (r3, r6)", async () => {
    const snapshot = chatStore.getSnapshot();
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({
      ...snapshot, sessions: [...snapshot.sessions, { id: "elsewhere", title: "Release planning", messages: [] } as never],
    });
    const row = wideRow("dir-i1", { quote: "Always summarise before you stop", created_at: "2026-11-18T09:40:00+00:00" });
    setItems([row]);
    workState({ section: "instructions", itemId: "directive:dir-i1", filters: { thisChat: true } });
    await render("rail", "here");

    const pane = detail()!;
    expect([...pane.querySelectorAll(".work-detail__header .pl-badge")].map((badge) => badge.textContent)).toEqual(["Instance-wide", "Instruction"]);
    const field = (label: string) => [...pane.querySelectorAll(".work-detail__field")]
      .find((node) => node.querySelector("dt")?.textContent === label)?.querySelector("dd")?.textContent;
    expect(field("Applies to")).toBe("Every chat and every unattended turn on this instance: scheduled jobs, watches, the inbox and webhooks.");
    expect(field("Your words")).toMatch(/^You said “Always summarise before you stop” in “Release planning” · .+ \d{2}:\d{2} \(your time\)$/);
    expect(field("Lasts")).toBe("Until you withdraw it");
    expect(buttons("Edit")).toHaveLength(1);
    expect(buttons("Withdraw…")).toHaveLength(1);
    expect(buttons("Withdraw…")[0].hasAttribute("data-withdraw")).toBe(true);
    expect(buttons("Withdraw…")[0].classList.contains("pl-btn--danger")).toBe(true);

    const summary = [...pane.querySelectorAll<HTMLElement>("button")].find((node) => node.textContent?.trim() === "Technical details")!;
    // The shared compact toggle, closed by default — not the bordered DS Accordion.
    expect(summary.classList.contains("technical-details__toggle")).toBe(true);
    expect(summary.getAttribute("aria-expanded")).toBe("false");
    expect(pane.querySelector(".work-detail__tech-row")).toBeNull();
    const focusedBefore = document.activeElement;
    await act(async () => summary.click());
    expect(summary.getAttribute("aria-expanded")).toBe("true");
    const tech = [...pane.querySelectorAll(".work-detail__tech-row")].map((node) =>
      [node.querySelector("dt")?.textContent, node.querySelector("dd")?.textContent]);
    expect(tech).toEqual([
      ["id", "dir-i1"], ["instance_wide", "true"], ["recorded in", "elsewhere"],
      ["status", "in_force"], ["created_at", "2026-11-18T09:40:00+00:00"],
    ]);

    // Opening the detail leaves focus on its heading — never on Withdraw… (the wasConfirming guard).
    expect(focusedBefore).toBe(pane.querySelector("h2"));
  });

  it("says the recording chat has since been deleted when this console no longer holds it", async () => {
    setItems([wideRow("dir-i1")]);
    workState({ section: "instructions", itemId: "directive:dir-i1" });
    await render("rail", "here");
    expect(detail()!.textContent).toContain("You said “Order dir-i1” in a chat that's since been deleted");
  });

  it("confirms with the instance-wide copy; Keep it and Escape both return focus to Withdraw… (r6)", async () => {
    setItems([wideRow("dir-i1")]);
    await openConfirm("dir-i1");

    const confirm = host.querySelector<HTMLElement>(".work-detail__confirm")!;
    const prompt = confirm.querySelector<HTMLElement>(".work-detail__confirm-prompt")!;
    expect(prompt.textContent).toBe("Withdraw this standing order everywhere?");
    expect(document.activeElement).toBe(prompt);
    expect(confirm.getAttribute("role")).toBe("group");
    expect(confirm.getAttribute("aria-labelledby")).toBe(prompt.id);
    expect(confirm.textContent).toContain("Every chat and every unattended turn stops following it from its next turn. Anything it already scheduled keeps running until you cancel it.");
    expect(confirm.querySelector("textarea")?.getAttribute("placeholder")).toBe("For example: moving the summary to the weekly digest");
    expect([...confirm.querySelectorAll(".work-detail__confirm-actions button")].map((b) => b.textContent)).toEqual(["Withdraw everywhere", "Keep it"]);

    await act(async () => buttons("Keep it")[0].click());
    expect(host.querySelector(".work-detail__confirm")).toBeNull();
    expect(document.activeElement).toBe(buttons("Withdraw…")[0]);

    await act(async () => buttons("Withdraw…")[0].click());
    await act(async () => {
      host.querySelector(".work-detail__confirm-prompt")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    });
    expect(host.querySelector(".work-detail__confirm")).toBeNull();
    expect(detail()).not.toBeNull(); // Escape closed the confirm, not the detail
    expect(document.activeElement).toBe(buttons("Withdraw…")[0]);
    expect(withdrawApi).not.toHaveBeenCalled();
  });

  it("puts Keep it first on the phone (the existing frame rule)", async () => {
    setItems([wideRow("dir-i1")]);
    workState({ section: "instructions", itemId: "directive:dir-i1" });
    await render("sheet", "here");
    await act(async () => buttons("Withdraw…")[0].click());
    expect([...host.querySelectorAll(".work-detail__confirm-actions button")].map((b) => b.textContent)).toEqual(["Keep it", "Withdraw everywhere"]);
  });

  it("Withdraw everywhere posts the reason, drops the row and count, and focuses the next row (r4, r6)", async () => {
    setItems([wideRow("dir-i1"), wideRow("dir-i2"), directiveRow("mine", { session_id: "here", quote: "Chat instruction" })]);
    await openConfirm("dir-i1");
    expect(heading()?.getAttribute("aria-label")).toBe("Your instructions, 3");
    await act(async () => type(host.querySelector("textarea")!, "moving it to the digest"));
    await act(async () => buttons("Withdraw everywhere")[0].click());

    expect(withdrawApi).toHaveBeenCalledWith("dir-i1", "moving it to the digest");
    expect(detail()).toBeNull();
    expect(rows().map((row) => row.querySelector(".work-row__title")?.textContent)).toEqual(["Order dir-i2", "Chat instruction"]);
    expect(heading()?.getAttribute("aria-label")).toBe("Your instructions, 2");
    expect(document.activeElement).toBe(rowFor("Order dir-i2"));
    // One announcement (§3.4): its own polite region says it, the list region's count stays
    // unsaid, and there is no toast.
    expect(withdrawnRegion().textContent).toBe("Withdrawn everywhere.");
    expect(listRegion().textContent).toBe("Your instructions, 3 items.");
    expect(document.querySelector(".pl-toast__msg")).toBeNull();
  });

  it("announces each of two withdraws in a row by replacing the region's text node", async () => {
    setItems([wideRow("dir-i1"), wideRow("dir-i2")]);
    await openConfirm("dir-i1");
    expect(withdrawnRegion().textContent).toBe("");
    await act(async () => buttons("Withdraw everywhere")[0].click());
    const region = withdrawnRegion();
    const first = region.firstElementChild;
    expect(region.textContent).toBe("Withdrawn everywhere.");

    await act(async () => rowFor("Order dir-i2")!.click());
    await act(async () => buttons("Withdraw…")[0].click());
    await act(async () => buttons("Withdraw everywhere")[0].click());
    expect(rows()).toHaveLength(0);
    expect(withdrawnRegion()).toBe(region);
    expect(region.textContent).toBe("Withdrawn everywhere.");
    expect(region.firstElementChild).not.toBe(first);
    expect(listRegion().textContent).toBe("Your instructions, 2 items.");
    expect(document.querySelector(".pl-toast__msg")).toBeNull();
  });

  it("falls back to the toast on a host that passes no withdraw announcement", async () => {
    const row = wideRow("dir-i1");
    setItems([row]);
    const onBack = vi.fn();
    await act(async () => root.render(<ToastProvider>{sections.get("directives")!.render({
      sources: data.sources, state: { itemId: "directive:dir-i1" }, onOpen: vi.fn(), onBack, sessionId: "here",
    })}</ToastProvider>));
    await act(async () => buttons("Withdraw…")[0].click());
    await act(async () => buttons("Withdraw everywhere")[0].click());
    expect(onBack).toHaveBeenCalled();
    expect(document.querySelector(".pl-toast__msg")?.textContent).toContain("Withdrawn.");
  });

  it("focuses the previous row when the withdrawn order was last (r6)", async () => {
    setItems([wideRow("dir-i1"), wideRow("dir-i2")]);
    await openConfirm("dir-i2");
    await act(async () => buttons("Withdraw everywhere")[0].click());
    expect(withdrawApi).toHaveBeenCalledWith("dir-i2", "");
    expect(rows()).toHaveLength(1);
    expect(document.activeElement).toBe(rowFor("Order dir-i1"));
  });

  it("focuses the group heading when the group is now empty — never body (r6)", async () => {
    setItems([wideRow("dir-i1")]);
    await openConfirm("dir-i1");
    await act(async () => buttons("Withdraw everywhere")[0].click());
    expect(rows()).toHaveLength(0);
    expect(heading()?.getAttribute("aria-label")).toBe("Your instructions, 0");
    expect(heading()?.getAttribute("tabindex")).toBe("-1");
    expect(document.activeElement).toBe(heading());
  });

  it("on 409 keeps the detail mounted read-only with the closing sentence, focused (r5)", async () => {
    const row = wideRow("dir-i1");
    setItems([row]);
    await openConfirm("dir-i1");
    withdrawApi.mockImplementation(async () => {
      setItems([closedRow(row)]); // closed elsewhere: the refetched list still carries it
      throw new ApiError(409, "already closed");
    });
    await act(async () => buttons("Withdraw everywhere")[0].click());

    const pane = detail()!;
    expect(pane.textContent).toMatch(/This was already closed: Withdrawn by you · today at .+ \(your time\)\. Nothing changed\./);
    expect(document.activeElement).toBe(pane.querySelector(".work-instruction-detail"));
    expect(buttons("Withdraw everywhere")).toHaveLength(0);
    expect(buttons("Withdraw…")).toHaveLength(0);
    // The closed row left the group (the hand-driven source needs a render to show it) while
    // the notice stays put.
    await render("rail", "here");
    expect(heading()?.getAttribute("aria-label")).toBe("Your instructions, 0");
    expect(detail()!.textContent).toContain("This was already closed:");
  });

  it("on 404 toasts that the order is gone and the list was refreshed (r5)", async () => {
    setItems([wideRow("dir-i1")]);
    await openConfirm("dir-i1");
    withdrawApi.mockImplementation(async () => {
      setItems([]);
      throw new ApiError(404, "no instance-wide directive");
    });
    await act(async () => buttons("Withdraw everywhere")[0].click());
    expect(document.querySelector(".pl-toast__msg")?.textContent).toBe("This order is no longer in the list. The list has been refreshed.");
  });

  it("on a network or 5xx error warns it is still in force everywhere, with Try again (r5)", async () => {
    setItems([wideRow("dir-i1")]);
    await openConfirm("dir-i1");
    withdrawApi.mockRejectedValueOnce(new ApiError(503, "unavailable"));
    await act(async () => buttons("Withdraw everywhere")[0].click());

    const alert = host.querySelector(".work-detail__confirm .pl-alert, .work-detail__confirm [role='alert']") ?? host.querySelector(".work-detail__confirm")!;
    expect(alert.textContent).toContain("Couldn't withdraw it. It's still in force everywhere.");
    expect(detail()).not.toBeNull();
    expect(rows()).toHaveLength(1);

    // Try again goes through the same route and, this time, lands.
    await act(async () => buttons("Try again")[0].click());
    expect(withdrawApi).toHaveBeenCalledTimes(2);
    expect(detail()).toBeNull();
    expect(document.activeElement).toBe(heading());
  });

  it("shows the stale callout above an instance-wide detail when the list couldn't refresh", async () => {
    setItems([wideRow("dir-i1")]);
    data.sources.directives.status = "stale";
    workState({ section: "instructions", itemId: "directive:dir-i1" });
    await render("rail", "here");
    expect(detail()!.textContent).toContain("Showing the last load. Couldn't refresh.");
    expect(buttons("Withdraw…")).toHaveLength(1);
  });

  it("leaves a chat instruction's own copy and route untouched", async () => {
    setItems([directiveRow("mine", { session_id: "here", quote: "Chat instruction" })]);
    dir.revoke.mockResolvedValue({ ok: true });
    workState({ section: "instructions", itemId: "directive:mine", filters: { thisChat: true } });
    await render("rail", "here");
    expect(detail()!.textContent).not.toContain("Instance-wide");
    expect(detail()!.textContent).not.toContain("Technical details");
    await act(async () => buttons("Withdraw…")[0].click());
    expect(host.querySelector(".work-detail__confirm-prompt")?.textContent).toBe("Withdraw this instruction?");
    await act(async () => buttons("Keep it")[0].click());
    expect(document.activeElement).toBe(buttons("Withdraw…")[0]); // the cancel() focus fix
    await act(async () => buttons("Withdraw…")[0].click());
    await act(async () => buttons("Withdraw")[0].click());
    expect(dir.revoke).toHaveBeenCalledWith("mine", "");
    expect(withdrawApi).not.toHaveBeenCalled();
    // The chat path keeps its toast and never uses the withdraw region.
    expect(document.querySelector(".pl-toast__msg")?.textContent).toBe("Withdrawn. The agent won't treat it as approved from its next turn.");
    expect(withdrawnRegion().textContent).toBe("");
  });
});


it("uses actual phone action sizes in decision and directive custom details", async () => {
  data.sources.decisions.items = [decisionItem(decisionRow("phone"))];
  workState({ section: "decisions", itemId: "decision:phone" });
  await render("sheet");
  expect(host.querySelectorAll(".work-decision-detail button").length).toBeGreaterThan(0);
  expect(host.querySelectorAll(".work-decision-detail button.pl-btn--sm")).toHaveLength(0);
  expect(host.querySelector(".work-view__back")?.classList.contains("pl-btn--sm")).toBe(false);
  data.sources.directives.items = [directiveItem(directiveRow("phone", { session_id: "here" }))];
  workState({ section: "directives", itemId: "directive:phone" });
  await render("sheet", "here");
  await act(async () => buttons("Withdraw…")[0].click());
  expect(host.querySelectorAll(".work-instruction-detail button.pl-btn--sm")).toHaveLength(0);
  expect([...host.querySelectorAll(".work-detail__confirm-actions button")].map((button) => button.textContent)).toEqual(["Keep it", "Withdraw"]);
});

it("registers decisions and instructions with canonical group metadata", () => {
  expect(sections.get("decisions")).toMatchObject({ label: "Decisions", group: "needs-you" });
  expect(sections.get("directives")).toMatchObject({ label: "Your instructions", group: "instructions" });
});
