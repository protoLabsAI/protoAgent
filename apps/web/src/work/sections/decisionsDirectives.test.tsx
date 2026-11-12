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
import { sections } from "../sectionRegistry";
import { decisionMeta, fromDecision, fromDirective } from "../tone";
import { useUI } from "../../state/uiStore";
import type { WorkItem, WorkItems, WorkSource, WorkSourceKind } from "../useWorkItems";
import type { DecisionRow } from "../../lib/api/decisions";
import type { DirectiveRow } from "../../lib/api/chat";
import type { DirectivesSnapshot } from "../../chat/useDirectives";

// Import only WorkView: production must register its built-in sections itself.

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The Work view's data hook, driven by hand.
const data = vi.hoisted(() => ({ sources: {} as WorkItems }));
vi.mock("../useWorkItems", () => ({ useWorkItems: () => data.sources }));

// The directive ledger's store (used by the reused DirectiveDrawerBody), driven by hand.
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

const connection = vi.hoisted(() => ({ online: true, listeners: new Set<(online: boolean) => void>() }));
vi.mock("../../lib/events", () => ({
  isConnected: () => connection.online,
  onConnectionChange: (listener: (online: boolean) => void) => {
    connection.listeners.add(listener);
    listener(connection.online);
    return () => connection.listeners.delete(listener);
  },
  onTopic: () => () => {},
}));

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
  vi.spyOn(decisionsApi, "listDecisions").mockImplementation(async () => {
    const decisions = data.sources.decisions.items.map((item) => item.raw as DecisionRow).filter((row) => row.state === "pending");
    return { decisions, pending: decisions.length };
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
    if (fails) expect(document.body.textContent).toContain("Couldn't open source chat");
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

describe("Directives section", () => {
  it.each(["directive:other", "other"])("opens the selected owner's ledger and withdrawal action (%s)", async (itemId) => {
    const here = directiveRow("focused", { session_id: "here", quote: "Focused chat instruction" });
    const other = directiveRow("other", { session_id: "elsewhere", quote: "Other chat instruction" });
    data.sources.directives.items = [directiveItem(here), directiveItem(other)];
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

  it("lists instruction rows, and the opened detail keeps the ledger's neutral 'Active' group (r3)", async () => {
    data.sources.directives.items = [directiveItem(directiveRow("a1", { session_id: "here", quote: "Hold the release until Monday" }))];
    dir.snapshot = {
      state: "ready",
      active: [directiveRow("a1", { quote: "Hold the release until Monday" })],
      history: [directiveRow("h1", { quote: "Cut the patch release", status: "fulfilled", closed_by: "agent", closed_at: "2026-11-07T19:00:00+00:00" })],
      loadedAt: 1,
    };
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail");

    // The Your instructions group row carries the kind tag — never the running/waiting word.
    const row = rowFor("Hold the release until Monday")!;
    expect(row.querySelector(".pl-badge")?.textContent).toBe("Instruction");
    expect(host.textContent).not.toMatch(/\brunning\b/);
    expect(host.textContent).not.toMatch(/\bwaiting\b/);

    // Opening the row keeps the preserved ledger detail: in-force stays the ledger's word
    // "Active" (the §2.11 exception) with history in its own collapsed group.
    workState({ section: "directives", itemId: "directive:a1", filters: { thisChat: true } });
    await render("rail");
    const detail = host.querySelector(".work-view__detail")!;
    expect(detail.textContent).toContain("Active");
    expect(detail.textContent).toContain("Hold the release until Monday");
    const historyTrigger = buttons("History (1)")[0];
    expect(historyTrigger).toBeTruthy();
    await act(async () => historyTrigger.click());
    expect(detail.textContent).toContain("Cut the patch release");
    expect([...detail.querySelectorAll(".pl-badge")].some((b) => b.textContent === "done")).toBe(true);
  });

  it("shows the ledger's own empty state for a session with nothing recorded", async () => {
    data.sources.directives.items = [];
    dir.snapshot = { state: "ready", active: [], history: [], loadedAt: 1 };
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail"); // sessionId comes from the section context
    expect(host.textContent).toContain("No standing instructions");
  });

  it("falls back to its own empty copy when no session is in view", async () => {
    data.sources.directives.items = [];
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail", undefined); // no session: the ledger body never mounts
    expect(host.textContent).toContain("No standing instructions");
  });

  it("never shows a directive id in the rows or the ledger detail (r4)", async () => {
    data.sources.directives.items = [directiveItem(directiveRow("secret-dir-id", { session_id: "here", quote: "Approve dependency bumps" }))];
    dir.snapshot = {
      state: "ready",
      active: [directiveRow("secret-dir-id", { quote: "Approve dependency bumps" })],
      history: [],
      loadedAt: 1,
    };
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail");
    expect(host.textContent).toContain("Approve dependency bumps");
    expect(host.textContent).not.toContain("secret-dir-id");
    workState({ section: "directives", itemId: "directive:secret-dir-id", filters: { thisChat: true } });
    await render("rail");
    expect(host.querySelector(".work-view__detail")?.textContent).toContain("Approve dependency bumps");
    expect(host.textContent).not.toContain("secret-dir-id");
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
});


it("uses actual phone action sizes in decision and directive custom details", async () => {
  data.sources.decisions.items = [decisionItem(decisionRow("phone"))];
  workState({ section: "decisions", itemId: "decision:phone" });
  await render("sheet");
  expect(host.querySelectorAll(".work-decision-detail button").length).toBeGreaterThan(0);
  expect(host.querySelectorAll(".work-decision-detail button.pl-btn--sm")).toHaveLength(0);
  expect(host.querySelector(".work-view__back")?.classList.contains("pl-btn--sm")).toBe(false);
  dir.snapshot = { state: "ready", active: [directiveRow("phone")], history: [], loadedAt: 1 };
  data.sources.directives.items = [directiveItem(directiveRow("phone"))];
  workState({ section: "directives", itemId: "directive:phone" });
  await render("sheet");
  await act(async () => buttons("Withdraw…")[0].click());
  expect(host.querySelectorAll(".chat-directive button.pl-btn--sm")).toHaveLength(0);
  expect([...host.querySelectorAll(".chat-directive-actions button")].map(button => button.textContent)).toEqual(["Keep it", "Withdraw"]);
});

it("registers decisions and instructions with canonical group metadata", () => {
  expect(sections.get("decisions")).toMatchObject({ label: "Decisions", group: "needs-you" });
  expect(sections.get("directives")).toMatchObject({ label: "Your instructions", group: "instructions" });
});
