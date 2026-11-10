import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "@protolabsai/ui/overlays";

import { WorkView } from "../WorkView";
import { decisionMeta, fromDecision, fromDirective } from "../tone";
import { useUI } from "../../state/uiStore";
import type { WorkItem, WorkItems, WorkSource, WorkSourceKind } from "../useWorkItems";
import type { DecisionRow } from "../../lib/api/decisions";
import type { DirectiveRow } from "../../lib/api/chat";
import type { DirectivesSnapshot } from "../../chat/useDirectives";

// Registers the two sections under test as a side effect of import.
import "./decisions";
import "./directives";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The Work view's data hook, driven by hand.
const data = vi.hoisted(() => ({ sources: {} as WorkItems }));
vi.mock("../useWorkItems", () => ({ useWorkItems: () => data.sources }));

// The directive ledger's store (used by the reused DirectiveDrawerBody), driven by hand.
const dir = vi.hoisted(() => ({
  snapshot: { state: "ready", active: [], history: [], loadedAt: 1 } as DirectivesSnapshot,
  refresh: vi.fn(),
  revoke: vi.fn(),
}));
vi.mock("../../chat/useDirectives", () => ({
  useDirectives: () => ({ ...dir.snapshot, refresh: dir.refresh, revoke: dir.revoke }),
}));
vi.mock("../../lib/useIsMobile", () => ({ useIsMobile: () => false }));

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

async function render(frame: "rail" | "modal" = "rail", sessionId: string | undefined = "here") {
  await act(async () => root.render(<ToastProvider><WorkView frame={frame} sessionId={sessionId} /></ToastProvider>));
}
function workState(patch: Record<string, unknown>) {
  useUI.setState({ surfaceStates: { work: patch }, surfaceModal: null });
}
const rows = () => [...host.querySelectorAll<HTMLElement>(".work-row")];
const rowFor = (text: string) => rows().find((r) => r.textContent?.includes(text));
const buttons = (text: string) => [...document.querySelectorAll<HTMLButtonElement>("button")].filter((b) => b.textContent?.trim() === text);

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  data.sources = emptySources();
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
  it("pins pending first with 'Asked {time}' and opens the DecisionCard as its detail (r1)", async () => {
    data.sources.decisions.items = [
      decisionItem(decisionRow("closed", { state: "answered", answer_label: "Go ahead", answered_by: "operator", answered_at: "2026-11-08T15:00:00+00:00" })),
      decisionItem(decisionRow("live", { created_at: new Date(Date.now() - 4 * 60_000).toISOString() })),
    ];
    workState({ section: "decisions", filters: { thisChat: true } });
    await render("rail");

    // Pending sorts ahead of the already-answered one, even though it came second in the list.
    expect(rows()[0].textContent).toContain("Question live");
    expect(rows()[0].textContent).toContain("Asked 4 min ago");
    expect(rows()[1].textContent).toContain("Question closed");

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
    // A running task fills the Doing lane; the pending decision must still sit above it (§1).
    data.sources.tasks.items = [{ kind: "task", id: "t1", status: "running", title: "Build the thing", meta: "", raw: {} }];
    workState({ section: "overview", filters: { thisChat: false } });
    await render("modal");

    const lanes = [...host.querySelectorAll("section")];
    // "Needs you" is the first lane, and its first (pinned) row is the pending decision with
    // the §2.4 field-3 pin — "Asked {time}" — not the agent name the old pin carried.
    expect(lanes[0].querySelector("h3")?.textContent).toBe("Needs you");
    const firstRow = lanes[0].querySelector(".work-row");
    expect(firstRow?.textContent).toContain("Question live");
    expect(firstRow?.textContent).toContain("Asked 4 min ago");
    expect(lanes[1].querySelector("h3")?.textContent).toBe("Doing"); // sits below Needs you

    // Opening the pin straight from the Overview renders the unchanged DecisionCard as its detail.
    workState({ section: "overview", itemId: "decision:live", filters: { thisChat: false } });
    await render("modal");
    const detail = host.querySelector(".work-view__detail");
    expect(detail?.textContent).toContain("Question live");
    expect(detail?.textContent).toContain("Reply in chat instead"); // a DecisionCard-only affordance
    expect(detail?.querySelector('[role="group"][aria-label="Answer"]')).toBeTruthy();
  });

  it("maps closed states to cancelled and shows 'Answered: {label}' for answered ones (r2)", async () => {
    data.sources.decisions.items = [
      decisionItem(decisionRow("ans", { state: "answered", answer_label: "Merge it", answered_by: "operator", answered_at: "2026-11-08T15:00:00+00:00" })),
      decisionItem(decisionRow("exp", { state: "expired" })),
      decisionItem(decisionRow("wd", { state: "withdrawn", withdraw_reason: "No longer needed" })),
      decisionItem(decisionRow("can", { state: "cancelled" })),
    ];
    workState({ section: "decisions", filters: { thisChat: true } });
    await render("rail");

    const answered = rowFor("Question ans")!;
    expect(answered.textContent).toContain("Answered: Merge it · by you");
    expect(answered.querySelector(".pl-badge")?.textContent).toBe("done");

    for (const [title, meta] of [["Question exp", null], ["Question wd", "Withdrawn: No longer needed"], ["Question can", null]] as const) {
      const row = rowFor(title)!;
      expect(row.querySelector(".pl-badge")?.textContent).toBe("cancelled");
      if (meta) expect(row.textContent).toContain(meta);
    }
  });

  it("shows the agent only in the All chats filter (r1 §2.4 field 5)", async () => {
    data.sources.decisions.items = [decisionItem(decisionRow("p", { session_id: "here" }))];

    workState({ section: "decisions", filters: { thisChat: true } });
    await render("rail");
    expect(host.querySelector(".work-row")?.textContent).not.toContain("protoEngineer");

    workState({ section: "decisions", filters: { thisChat: false } });
    await render("modal");
    expect(host.querySelector(".work-row")?.textContent).toContain("protoEngineer");
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

describe("Directives section", () => {
  it("renders the ledger with in-force entries under a neutral 'Active' group (r3)", async () => {
    data.sources.directives.items = [directiveItem(directiveRow("a1", { session_id: "here" }))];
    dir.snapshot = {
      state: "ready",
      active: [directiveRow("a1", { quote: "Hold the release until Monday" })],
      history: [directiveRow("h1", { quote: "Cut the patch release", status: "fulfilled", closed_by: "agent", closed_at: "2026-11-07T19:00:00+00:00" })],
      loadedAt: 1,
    };
    workState({ section: "directives", filters: { thisChat: true } });
    await render("rail");

    // In-force keeps the ledger's word "Active" (the §2.11 exception), never the running/waiting word.
    expect(host.textContent).toContain("Active");
    expect(host.textContent).toContain("Hold the release until Monday");
    expect(host.textContent).not.toMatch(/\brunning\b/);
    expect(host.textContent).not.toMatch(/\bwaiting\b/);

    // History sits in the ledger's own collapsed group with its own done/withdrawn badges.
    const historyTrigger = buttons("History (1)")[0];
    expect(historyTrigger).toBeTruthy();
    await act(async () => historyTrigger.click());
    const done = rows().length; // no WorkRows in the ledger view
    expect(done).toBe(0);
    expect(host.textContent).toContain("Cut the patch release");
    expect([...host.querySelectorAll(".pl-badge")].some((b) => b.textContent === "done")).toBe(true);
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

  it("never shows a directive id in the ledger (r4)", async () => {
    data.sources.directives.items = [directiveItem(directiveRow("secret-dir-id", { session_id: "here" }))];
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
  });

  it("routes closed directives to Done and in-force to Waiting on in the Overview counts only (r3 §2.11)", async () => {
    data.sources.directives.items = [
      directiveItem(directiveRow("inforce")),
      directiveItem(directiveRow("fulfilled", { status: "fulfilled", closed_at: new Date().toISOString() })),
      directiveItem(directiveRow("revoked", { status: "revoked", closed_at: new Date().toISOString() })),
    ];
    workState({ section: "overview", filters: { thisChat: false } });
    await render("modal");
    const lanes = [...host.querySelectorAll("section h3")].map((n) => n.textContent);
    expect(lanes).toContain("Waiting on"); // in_force → waiting
    expect(lanes).toContain("Done"); // fulfilled → done, revoked → cancelled
  });
});
