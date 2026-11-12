// The Goals and Watches Work sections (#4955 §2.5–2.6), mounted for real inside the DS
// ToastProvider + a QueryClientProvider so the undo toast, the inline confirm and the edit form
// all run through the DOM. Only the action API calls, the New-goal dialog and the HitlForm body
// are stubbed; the field tables, their order and the "never shown" rules are exercised live.

import { createElement as h, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";

// Mocks are hoisted above the section imports so the import side-effect registers against the
// spy, and the heavy GoalsPanel / HitlForm trees never load.
const { registrations } = vi.hoisted(() => ({
  registrations: [] as Array<[string, { label: string; render: (ctx: unknown) => ReactNode }]>,
}));
vi.mock("../WorkView", () => ({
  registerWorkSection: (id: string, section: { label: string; render: (ctx: unknown) => ReactNode }) => {
    registrations.push([id, section]);
    return () => {};
  },
}));
vi.mock("../sectionRegistry", () => ({
  registerWorkSection: (id: string, section: { label: string; render: (ctx: unknown) => ReactNode }) => {
    registrations.push([id, section]);
    return () => {};
  },
}));
vi.mock("../../app/GoalsPanel", () => ({
  GoalCreateDialog: ({ open, onClose }: { open: boolean; onClose: () => void }) =>
    open ? h("div", { "data-testid": "goal-create-dialog" }, h("button", { onClick: onClose }, "Cancel creation")) : null,
}));
vi.mock("../../chat/HitlForm", () => ({
  HitlForm: ({ onSubmit }: { onSubmit: (a: Record<string, unknown>) => void }) =>
    h(
      "button",
      {
        type: "button",
        onClick: () => onSubmit({ outcome: "Edited outcome", mode: "repeat", run_prompt: "ping me", interval: "2h", stall_after: 4 }),
      },
      "save-edit",
    ),
}));

import { useUI } from "../../state/uiStore";
import { applyNavIntent } from "../../app/palette/nav";
import { api } from "../../lib/api";
import { goalsSection, useGoalCreator } from "./goals";
import { watchesSection } from "./watches";
import type { WorkItem, WorkItems, WorkSource } from "../useWorkItems";
import type { GoalState, WatchState } from "../../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;

async function mount(node: ReactNode) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root!.render(h(QueryClientProvider, { client: qc }, h(ToastProvider, null, node) as ReactNode));
  });
}
async function flush() {
  await act(async () => {});
}
function buttons(): HTMLButtonElement[] {
  return Array.from(host!.querySelectorAll("button"));
}
function button(label: string): HTMLButtonElement {
  const found = buttons().find((b) => b.textContent?.trim() === label);
  if (!found) throw new Error(`no button "${label}" (have: ${buttons().map((b) => b.textContent?.trim()).join(", ")})`);
  return found;
}
function text(): string {
  return host!.textContent ?? "";
}
function labels(): (string | null)[] {
  return Array.from(host!.querySelectorAll(".work-detail__field dt")).map((n) => n.textContent);
}

function emptySrc(): WorkSource {
  return { items: [], status: "ready", loadedAt: 1, error: null, retry: vi.fn() };
}
function sources(over: Partial<WorkItems>): WorkItems {
  const base: WorkItems = {
    decisions: emptySrc(),
    goals: emptySrc(),
    watches: emptySrc(),
    tasks: emptySrc(),
    schedules: emptySrc(),
    background: emptySrc(),
    activity: emptySrc(),
    directives: emptySrc(),
  };
  return { ...base, ...over };
}
function withItems(items: WorkItem[]): WorkSource {
  return { ...emptySrc(), items };
}
function goalItem(raw: GoalState, status: WorkItem["status"] = "running"): WorkItem {
  return { kind: "goal", id: raw.session_id, status, title: raw.outcome?.trim() || raw.condition, meta: "", raw };
}
function watchItem(raw: WatchState, status: WorkItem["status"] = "waiting"): WorkItem {
  return { kind: "watch", id: raw.id, status, title: raw.condition, meta: "", raw };
}
// Loose on purpose — the sections read only sources/state/onOpen.
function ctx(over: { sources?: WorkItems; itemId?: string }): never {
  return {
    sources: over.sources ?? sources({}),
    state: { itemId: over.itemId },
    onOpen: vi.fn(),
    onBack: vi.fn(),
  } as never;
}

beforeEach(() => {
  root = null;
  host = null;
  useUI.setState({ surfaceStates: {}, surfaceModal: null });
});
afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.restoreAllMocks();
});

describe("registration (import side-effect)", () => {
  it("registers the Goals and Watches sections", () => {
    const ids = registrations.map(([id]) => id);
    expect(ids).toContain("goals");
    expect(ids).toContain("watches");
    expect(goalsSection.label).toBe("Goals");
    expect(watchesSection.label).toBe("Watches");
    expect(registrations.find(([id]) => id === "goals")?.[1]).toMatchObject({ group: "doing-now" });
    expect(registrations.find(([id]) => id === "watches")?.[1]).toMatchObject({ group: "coming-up" });
  });
});

describe("Goals detail (§2.5, r1)", () => {
  const fullGoal: GoalState = {
    session_id: "sess-abc",
    condition: "Make it",
    status: "active",
    verifier: { type: "command", command: "pytest -q" },
    outcome: "Ship the thing",
    constraints: ["keep the API stable"],
    boundaries: ["src/"],
    stop_when: "the build goes red",
    iteration: 3,
    max_iterations: 10,
    no_progress_streak: 2,
    no_progress_limit: 3,
    fresh_context: true,
    abandon_reason: "blocked upstream",
    last_reason: "still going",
    last_evidence: "evidence-blob",
    history: [{ iteration: 1, status: "continue", reason: "progress" }],
    started_at: 1_700_000_000,
    finished_at: 1_700_003_600,
  };

  it("renders the §2.5 fields in order, led by the finish line (r1)", async () => {
    await mount(goalsSection.render(ctx({ sources: sources({ goals: withItems([goalItem(fullGoal)]) }), itemId: "goal:sess-abc" })));
    expect(labels()).toEqual([
      "Finish line",
      "Progress",
      "Stalled",
      "Latest check",
      "Started",
      "Finished",
      "Stop rule",
      "Must not change",
      "In scope",
      "Why it stopped",
      "History",
      "Fresh context",
    ]);
    expect(text()).toContain("Ship the thing"); // the finish line
    expect(text()).toContain("step 3 of 10 · checking after each turn");
    expect(text()).toContain("No progress for 2 of 3 checks");
    expect(text()).toContain("Each turn starts a clean thread");
  });

  it("an unlimited goal shows 'step 3', never 'n/0' or '∞'", async () => {
    const g: GoalState = { session_id: "sess-u", condition: "Run forever", status: "active", outcome: "Keep watch", iteration: 3 };
    await mount(goalsSection.render(ctx({ sources: sources({ goals: withItems([goalItem(g)]) }), itemId: "goal:sess-u" })));
    expect(text()).toContain("step 3 · checking after each turn");
    expect(text()).not.toContain("step 3 of");
    expect(text()).not.toContain("∞");
    expect(text()).not.toMatch(/\/\s*0\b/);
  });
});

describe("Watches detail (§2.6, r1)", () => {
  const fullWatch: WatchState = {
    id: "watch-1",
    condition: "deploy finishes",
    status: "met",
    verifier: { type: "command", command: "kubectl rollout status" },
    interval_s: 300,
    deadline: 1_700_100_000,
    stall_after: 6,
    trigger: "met",
    check_count: 41,
    fire_count: 3,
    consecutive_fires: 3,
    repeat: false,
    run_prompt: "let me know",
    run_session: "operator",
    last_reason: "not yet",
    last_evidence: "eblob",
    last_checked: 1_700_000_000,
    finished_at: 1_700_003_600,
    created_at: 1_699_990_000,
  };

  it("renders the §2.6 fields in order, including the flapping signal", async () => {
    await mount(watchesSection.render(ctx({ sources: sources({ watches: withItems([watchItem(fullWatch)]) }), itemId: "watch:watch-1" })));
    expect(labels()).toEqual([
      "Kind",
      "Last checked",
      "Fired",
      "Then",
      "Deadline",
      "Latest check",
      "Cadence",
      "Stall alert",
      "Set",
      "Finished",
    ]);
    expect(text()).toContain("Fired 3 times · 3 in a row");
    expect(text()).toContain("Fires once when met");
    expect(text()).toContain("Checked");
    expect(text()).toContain("41 checks");
    expect(text()).toContain("Checks every 5 min");
  });
});

describe("Technical details only (r2)", () => {
  it("keeps a goal's session id, verifier type and raw status out of the visible fields", async () => {
    const g: GoalState = {
      session_id: "sess-xyz",
      condition: "Build it",
      status: "exhausted",
      verifier: { type: "command", command: "pytest" },
      outcome: "Build the thing",
      iteration: 2,
      last_reason: "almost",
      last_evidence: "ev",
    };
    await mount(goalsSection.render(ctx({ sources: sources({ goals: withItems([goalItem(g, "failed")]) }), itemId: "goal:sess-xyz" })));
    expect(text()).not.toContain("sess-xyz");
    expect(text()).not.toContain("exhausted");
    expect(text()).not.toContain("command");
    await act(async () => button("Technical details").click());
    expect(text()).toContain("sess-xyz");
    expect(text()).toContain("exhausted");
    expect(text()).toContain("command");
  });

  it("keeps a watch's id, verifier type and raw status out of the visible fields", async () => {
    const w: WatchState = {
      id: "watch-xyz",
      condition: "deploy finishes",
      status: "active",
      verifier: { type: "command" },
      trigger: "met",
      repeat: false,
      fire_count: 0,
      check_count: 5,
      last_checked: 1_700_000_000,
      run_session: "operator",
      last_evidence: "ev",
    };
    await mount(watchesSection.render(ctx({ sources: sources({ watches: withItems([watchItem(w, "waiting")]) }), itemId: "watch:watch-xyz" })));
    expect(text()).not.toContain("watch-xyz");
    expect(text()).not.toContain("command");
    // the badge word is "waiting", not the raw "active"
    expect(host!.querySelector(".work-detail__fields")?.textContent ?? "").not.toContain("active");
    await act(async () => button("Technical details").click());
    expect(text()).toContain("watch-xyz");
    expect(text()).toContain("command");
    expect(text()).toContain("active");
  });
});

describe("Delete and undo (r3)", () => {
  const goal: GoalState = { session_id: "sess-del", condition: "done goal", status: "achieved", outcome: "a finished goal", iteration: 4 };
  const watch: WatchState = { id: "watch-del", condition: "a watch", status: "active", trigger: "met", repeat: false };

  it("deleting a goal shows the Undo toast and Undo calls POST /api/undo/{token}", async () => {
    const clearGoal = vi.spyOn(api, "clearGoal").mockResolvedValue({ cleared: true, undo_token: "tok-goal" });
    const undo = vi.spyOn(api, "undo").mockResolvedValue({ restored: true, kind: "goal" });
    await mount(goalsSection.render(ctx({ sources: sources({ goals: withItems([goalItem(goal, "done")]) }), itemId: "goal:sess-del" })));

    await act(async () => button("Delete…").click());
    await act(async () => button("Delete").click());
    await flush();

    expect(clearGoal).toHaveBeenCalledWith("sess-del", false);
    expect(text()).toContain("Goal deleted.");

    await act(async () => button("Undo").click());
    await flush();
    expect(undo).toHaveBeenCalledWith("tok-goal");
  });

  it("deleting a watch shows the Undo toast and Undo calls POST /api/undo/{token}", async () => {
    const clearWatch = vi.spyOn(api, "clearWatch").mockResolvedValue({ cleared: true, undo_token: "tok-watch" });
    const undo = vi.spyOn(api, "undo").mockResolvedValue({ restored: true, kind: "watch" });
    await mount(watchesSection.render(ctx({ sources: sources({ watches: withItems([watchItem(watch)]) }), itemId: "watch:watch-del" })));

    await act(async () => button("Delete…").click());
    await act(async () => button("Delete").click());
    await flush();

    expect(clearWatch).toHaveBeenCalledWith("watch-del");
    expect(text()).toContain("Watch deleted.");

    await act(async () => button("Undo").click());
    await flush();
    expect(undo).toHaveBeenCalledWith("tok-watch");
  });
});

describe("Lifecycle re-arm (ADR 0079)", () => {
  it("an active goal offers Add 4 iterations, which POSTs rearm with add_iterations", async () => {
    const rearm = vi.spyOn(api, "rearmGoal").mockResolvedValue({ ok: true, message: "extended" });
    const goal: GoalState = { session_id: "sess-ext", condition: "extend me", status: "active", outcome: "active goal", iteration: 2, max_iterations: 10 };
    await mount(goalsSection.render(ctx({ sources: sources({ goals: withItems([goalItem(goal, "running")]) }), itemId: "goal:sess-ext" })));

    expect(buttons().some((b) => b.textContent?.trim() === "Restart goal")).toBe(false);
    await act(async () => button("Add 4 iterations").click());
    await flush();
    expect(rearm).toHaveBeenCalledWith("sess-ext", { add_iterations: 4 });
  });

  it("a stopped goal offers Keep going, which POSTs rearm with no added budget", async () => {
    const rearm = vi.spyOn(api, "rearmGoal").mockResolvedValue({ ok: true, resumed: true });
    const goal: GoalState = { session_id: "sess-exh", condition: "ran out", status: "exhausted", outcome: "exhausted goal", iteration: 10, max_iterations: 10 };
    await mount(goalsSection.render(ctx({ sources: sources({ goals: withItems([goalItem(goal, "failed")]) }), itemId: "goal:sess-exh" })));

    expect(buttons().some((b) => b.textContent?.trim() === "Add 4 iterations")).toBe(false);
    await act(async () => button("Keep going").click());
    await flush();
    expect(rearm).toHaveBeenCalledWith("sess-exh", {});
  });

  it("an achieved goal offers neither re-arm control", async () => {
    const goal: GoalState = { session_id: "sess-ok", condition: "done", status: "achieved", outcome: "achieved goal", iteration: 4 };
    await mount(goalsSection.render(ctx({ sources: sources({ goals: withItems([goalItem(goal, "done")]) }), itemId: "goal:sess-ok" })));
    const btnLabels = buttons().map((b) => b.textContent?.trim());
    expect(btnLabels).not.toContain("Add 4 iterations");
    expect(btnLabels).not.toContain("Keep going");
  });
});

describe("Stopped-short goal recovery (§2, r1)", () => {
  it("the detail reads neutral 'Goal stopped' with the finish line, step progress, Keep going and Drop the goal", async () => {
    const goal: GoalState = { session_id: "sess-stop", condition: "ran out", status: "exhausted", outcome: "Ship it", iteration: 6, max_iterations: 8 };
    await mount(goalsSection.render(ctx({ sources: sources({ goals: withItems([goalItem(goal, "failed")]) }), itemId: "goal:sess-stop" })));

    // The badge names the stopped kind, in a neutral tone — nothing uses error (§6).
    expect(host!.querySelector(".work-detail__header .pl-badge")?.textContent).toBe("Goal stopped");
    expect(host!.querySelector(".pl-badge--error")).toBeNull();
    expect(host!.querySelector(".pl-dot--error")).toBeNull();

    // Finish line + step progress; a stopped goal isn't "checking after each turn".
    expect(text()).toContain("Ship it");
    expect(text()).toContain("step 6 of 8");
    expect(text()).not.toContain("checking after each turn");

    const btnLabels = buttons().map((b) => b.textContent?.trim());
    expect(btnLabels).toContain("Keep going");
    expect(btnLabels).toContain("Drop the goal");
    expect(btnLabels).not.toContain("Restart goal");
    expect(btnLabels).not.toContain("Delete…");
    expect(btnLabels).not.toContain("Stop…"); // not an active goal
  });

  it("Drop the goal clears it and offers Undo (r1)", async () => {
    const clearGoal = vi.spyOn(api, "clearGoal").mockResolvedValue({ cleared: true, undo_token: "tok-stop" });
    const undo = vi.spyOn(api, "undo").mockResolvedValue({ restored: true, kind: "goal" });
    const goal: GoalState = { session_id: "sess-drop", condition: "x", status: "unachievable", outcome: "do it", iteration: 2 };
    await mount(goalsSection.render(ctx({ sources: sources({ goals: withItems([goalItem(goal, "failed")]) }), itemId: "goal:sess-drop" })));

    await act(async () => button("Drop the goal").click()); // opens the inline confirm
    await act(async () => button("Drop the goal").click()); // confirmLabel is also "Drop the goal"
    await flush();
    expect(clearGoal).toHaveBeenCalledWith("sess-drop", false);
    // The toast matches the action's verb — the goal was dropped, not deleted.
    expect(text()).toContain("Goal dropped.");
    expect(text()).not.toContain("Goal deleted.");

    await act(async () => button("Undo").click());
    await flush();
    expect(undo).toHaveBeenCalledWith("tok-stop");
  });

  // The stopped-goal ROW ("Goal stopped", neutral, "step N of M" meta) and the achieved
  // goal's disappearance are group-row behavior now — WorkView.test's approved-table test
  // covers them through the shared workGroups selector; this section only renders details.
});

describe("New goal (r4)", () => {
  // The creator's one live host is WorkView's GoalCreatorHost (`useGoalCreator(state).creator`,
  // the palette's "Work: New goal…" intent) — the v2 group rows offer no create control, so
  // this exercises the hook through that same seam.
  it("opens on mount and on repeated palette requests, clearing the flag on cancel", async () => {
    const openCreator = () => applyNavIntent({ kind: "surface", id: "work", state: { section: "goals", filters: { create: true, thisChat: false } } });
    function LiveGoals() {
      const state = useUI((s) => s.surfaceStates.work!);
      return useGoalCreator(state).creator;
    }
    openCreator();
    await mount(h(LiveGoals));
    expect(host!.querySelector('[data-testid="goal-create-dialog"]')).not.toBeNull();
    await act(async () => button("Cancel creation").click());
    expect(host!.querySelector('[data-testid="goal-create-dialog"]')).toBeNull();
    expect(useUI.getState().surfaceStates.work.filters).toEqual({ thisChat: false });
    expect(useUI.getState().surfaceModal!.state.filters).toEqual({ thisChat: false });
    await act(async () => openCreator());
    expect(host!.querySelector('[data-testid="goal-create-dialog"]')).not.toBeNull();
    await act(async () => button("Cancel creation").click());
    await act(async () => useUI.getState().openSurfaceModal("work"));
    expect(host!.querySelector('[data-testid="goal-create-dialog"]')).toBeNull();
  });
});

describe("Edit (PATCH)", () => {
  it("Edit opens the contract form and PATCHes the goal", async () => {
    const editGoal = vi.spyOn(api, "editGoal").mockResolvedValue({ goal: null });
    const goal: GoalState = { session_id: "sess-edit", condition: "edit me", status: "active", outcome: "editable", iteration: 1 };
    await mount(goalsSection.render(ctx({ sources: sources({ goals: withItems([goalItem(goal, "running")]) }), itemId: "goal:sess-edit" })));

    await act(async () => button("Edit").click());
    await act(async () => button("save-edit").click());
    await flush();

    expect(editGoal).toHaveBeenCalledWith("sess-edit", expect.objectContaining({ outcome: "Edited outcome" }));
  });

  it("Edit opens the cadence form and PATCHes the watch", async () => {
    const editWatch = vi.spyOn(api, "editWatch").mockResolvedValue({ ok: true });
    const watch: WatchState = { id: "watch-edit", condition: "edit me", status: "active", trigger: "met", repeat: false };
    await mount(watchesSection.render(ctx({ sources: sources({ watches: withItems([watchItem(watch)]) }), itemId: "watch:watch-edit" })));

    await act(async () => button("Edit").click());
    await act(async () => button("save-edit").click());
    await flush();

    expect(editWatch).toHaveBeenCalledWith("watch-edit", expect.objectContaining({ repeat: true, interval_s: 7200 }));
  });
});
