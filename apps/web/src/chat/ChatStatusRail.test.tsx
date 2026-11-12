/** Chat status rail (work consolidation v2 §5, S6a): exact chip names/order/counts from the
 *  SHARED workGroups selector with thisChat applied, grouped details with Work's own names and
 *  Open-in-Work deep links, the preserved instructions Load-again/error state, the one polite
 *  change-only live region, and the dialog a11y contract (haspopup/expanded, trap, Escape,
 *  focus return). The selector itself is covered in work/workGroups.test.ts — here every count
 *  assertion is checked AGAINST the selector so chip and Work-row numbers cannot drift. */
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useUI } from "../state/uiStore";
import type { WorkItems, WorkSource } from "../work/useWorkItems";
import { flattenWorkItems, workGroups } from "../work/workGroups";
import { registerWorkSection } from "../work/sectionRegistry";
import type { GroupableWorkItem } from "../work/tone";
import { beginThisChatVisit, endThisChatVisit, isThisChatVisit } from "../work/visitScope";
import { ChatStatusRail, openWorkGroup, spokenLabel, workSectionForGroup } from "./ChatStatusRail";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** The rail reads THIS chat's ledger straight from the per-session store (like the old
 *  DirectiveStrip), never the aggregated source's summed status. */
type LedgerState = {
  state: "loading" | "ready" | "failed" | "stale";
  loadedAt: number | null;
  refresh: ReturnType<typeof vi.fn>;
};

const state = vi.hoisted(() => ({
  mobile: false,
  sources: null as unknown as WorkItems,
  hookArgs: [] as unknown[],
  ledger: null as unknown as LedgerState,
  ledgerSessions: [] as string[],
}));
vi.mock("../lib/useIsMobile", () => ({ useIsMobile: () => state.mobile }));
vi.mock("../work/useWorkItems", () => ({
  useWorkItems: (args: unknown) => {
    state.hookArgs.push(args);
    return state.sources;
  },
}));
vi.mock("./useDirectives", () => ({
  useDirectives: (sessionId: string) => {
    state.ledgerSessions.push(sessionId);
    return { ...state.ledger, active: [], history: [], revoke: vi.fn() };
  },
}));

const SESSION = "s1";

function source(items: GroupableWorkItem[] = [], overrides: Partial<WorkSource> = {}): WorkSource {
  return { items: items as WorkSource["items"], status: "ready", loadedAt: 1, error: null, retry: vi.fn(), ...overrides };
}

/** All eight sources; group-relevant rows ride `groupItems` like the live hook's projections. */
function mkSources(items: GroupableWorkItem[], directiveOverrides: Partial<WorkSource> = {}): WorkItems {
  const directives = items.filter((item) => item.kind === "directive");
  const rest = items.filter((item) => item.kind !== "directive");
  return {
    decisions: source(), goals: source(), watches: source(), tasks: source(),
    schedules: source(), activity: source(),
    background: { ...source(), groupItems: rest },
    directives: { ...source(), groupItems: directives, ...directiveOverrides },
  };
}

type Raw = Record<string, unknown>;
const goal = (id: string, raw: Raw = {}): GroupableWorkItem => ({
  kind: "goal", id, status: "running", title: `Goal ${id}`, meta: "",
  sessionId: id, raw: { session_id: id, status: "active", iteration: 3, max_iterations: 8, ...raw },
});
const bg = (id: string, status: string, sessionId?: string): GroupableWorkItem => ({
  kind: "background", id, status: status === "running" ? "running" : "scheduled",
  title: `Job ${id}`, meta: "delegate", sessionId, raw: { status },
});
const task = (id: string, status: string, sessionId?: string, started?: boolean): GroupableWorkItem => ({
  kind: "task", id, status: "scheduled", title: `Task ${id}`, meta: "", sessionId, started, raw: { status },
});
const watch = (id: string): GroupableWorkItem => ({
  kind: "watch", id, status: "waiting", title: `Watch ${id}`, meta: "", raw: { status: "active" },
});
const schedule = (id: string, sessionId?: string): GroupableWorkItem => ({
  kind: "schedule", id, status: "scheduled", title: `Run ${id}`, meta: "daily", sessionId,
  raw: { schedule: "0 9 * * *", enabled: true, next_fire: "2027-01-01T00:00:00Z" },
});
const directive = (id: string, sessionId?: string, extra: { tiedJob?: { kind: "task" | "background" | "goal"; id: string } } = {}): GroupableWorkItem => ({
  kind: "directive", id, status: "waiting", title: `Quote ${id}`, meta: `scope ${id}`,
  sessionId, raw: { status: "in_force" }, ...extra,
});
const decision = (id: string): GroupableWorkItem => ({
  kind: "decision", id, status: "needs-you", title: `Decide ${id}`, meta: "", raw: { state: "pending" },
});

let host: HTMLDivElement;
let root: Root;
const initialUI = useUI.getState();

beforeEach(() => {
  vi.clearAllMocks();
  Object.assign(state, { mobile: false, hookArgs: [], ledgerSessions: [], ledger: { state: "ready", loadedAt: 1, refresh: vi.fn() } });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});
  Element.prototype.hasPointerCapture = Element.prototype.hasPointerCapture ?? (() => false);
  useUI.setState({
    ...initialUI,
    surface: "chat", mobileActive: "chat", rightPanel: "context", rightCollapsed: true,
    surfaceModal: null, railOrder: { left: ["chat"], right: ["work", "context"], bottom: [], hidden: [] },
    surfaceStates: { work: { section: "tasks", itemId: "task:old", scrollTop: 80, filters: { retained: true } } },
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  useUI.setState(initialUI);
  endThisChatVisit();
});

async function render(node: ReactElement) {
  await act(async () => root.render(node));
}
const rail = (items: GroupableWorkItem[], props: Partial<Parameters<typeof ChatStatusRail>[0]> = {},
  directiveOverrides: Partial<WorkSource> = {}) => {
  state.sources = mkSources(items, directiveOverrides);
  return (
    <ChatStatusRail sessionId={SESSION} cast={[]} draft="" onToggleMention={() => {}} {...props} />
  );
};
const chip = (id: string) => host.querySelector<HTMLButtonElement>(`[data-testid="chat-status-chip-${id}"]`);
const dialog = () => document.querySelector<HTMLElement>('[role="dialog"]');
async function click(el: Element | null | undefined) {
  expect(el).toBeTruthy();
  await act(async () => (el as HTMLElement).click());
}

const MIXED: GroupableWorkItem[] = [
  goal(SESSION),
  bg("bg-run", "running", SESSION),
  bg("bg-elsewhere", "running", "other-chat"), // another chat: not in this rail
  bg("bg-open", "open", SESSION), // not started: Coming up, never Doing now
  bg("bg-done", "completed", SESSION), // terminal: leaves Work, leaves the counts
  task("t-deferred", "deferred", SESSION), // not started: Coming up
  watch("w1"), // no owner session: outside the thisChat scope
  schedule("sch1", SESSION),
  directive("d1", SESSION),
  directive("d-elsewhere", "other-chat"),
  decision("q1"), // Needs you is S7-gated: no chip
];

describe("chips", () => {
  it("shows the exact §5 names, order, counts, goal progress and spoken labels from the shared selector", async () => {
    await render(rail(MIXED));
    const buttons = [...host.querySelectorAll<HTMLButtonElement>(".chat-status-chip")];
    expect(buttons.map((b) => b.dataset.testid)).toEqual([
      "chat-status-chip-doing-now", "chat-status-chip-coming-up", "chat-status-chip-instructions",
    ]);
    // Visible text, complete spoken label, and the phone split (the wordy __text span is the
    // one the phone media query hides; the __count stands alone). The rendered numbers ARE
    // the Work group rows: same selector, thisChat applied.
    const groups = workGroups(flattenWorkItems(state.sources), { thisChatOnly: true, sessionId: SESSION });
    const table: Array<[string, string, string]> = [
      ["doing-now", "Doing now · goal 3/8 · 2",
        "Doing now, 2 running in this chat, including the goal at step 3 of 8. Show details"],
      ["coming-up", "Coming up 3", "Coming up: 3 not started in this chat. Show details"],
      ["instructions", "Told it 1", "Told it: 1 instruction in force in this chat. Show details"],
    ];
    for (const [id, text, label] of table) {
      expect(chip(id)!.textContent).toBe(text);
      expect(chip(id)!.getAttribute("aria-label")).toBe(label);
      expect(chip(id)!.querySelector(".chat-status-chip__text")).toBeTruthy();
      expect(chip(id)!.querySelector(".chat-status-chip__count")!.textContent)
        .toBe(String(groups.find((g) => g.id === id)!.count));
    }
    // Needs you is S7-gated; the rail reads ALL chats' rows (scoping happens in the selector)
    // and THIS chat's instructions ledger from the per-session store.
    expect(host.querySelector('[data-testid="chat-status-chip-needs-you"]')).toBeNull();
    expect(state.hookArgs[0]).toEqual({ sessionId: SESSION, thisChatOnly: false });
    expect(state.ledgerSessions[0]).toBe(SESSION);
  });

  it("uses the spinner with no goal, pluralizes Told it, and keeps only started blocked work in Doing now", async () => {
    await render(rail([bg("bg-run", "running", SESSION), directive("d1", SESSION), directive("d2", SESSION)]));
    expect(chip("doing-now")!.textContent).toBe("Doing now 1");
    expect(chip("doing-now")!.querySelector(".pl-spinner")).toBeTruthy();
    expect(chip("instructions")!.getAttribute("aria-label"))
      .toBe("Told it: 2 instructions in force in this chat. Show details");
    await render(rail(MIXED));
    expect(chip("doing-now")!.querySelector(".pl-spinner")).toBeNull(); // goal → target icon
    // started: false is the selector's only "never ran" proof — absent means it did start.
    await render(rail([task("t-blocked", "blocked", SESSION, true), task("t-waiting", "blocked", SESSION, false)]));
    expect(chip("doing-now")!.textContent).toBe("Doing now 1");
    expect(chip("coming-up")!.textContent).toBe("Coming up 1");
  });

  it("hides zero-count chips and renders no rail at all when there is nothing to show", async () => {
    await render(rail([bg("bg-done", "completed", SESSION), decision("q1")]));
    expect(host.querySelector('[data-testid="chat-status-rail"]')).toBeNull();
  });
});

describe("details panel", () => {
  it("groups by the Work names with counts, deep links each group with thisChat", async () => {
    await render(rail(MIXED));
    await click(chip("instructions"));
    const panel = dialog();
    expect(panel).toBeTruthy();
    expect(chip("instructions")!.getAttribute("aria-haspopup")).toBe("dialog");
    expect(chip("instructions")!.getAttribute("aria-expanded")).toBe("true");

    // Work's own names — "Your instructions · N", never "Told it" — count in the accessible name.
    expect([...panel!.querySelectorAll("h2")].map((h) => [h.textContent, h.getAttribute("aria-label")])).toEqual([
      ["Doing now · 2", "Doing now, 2"], ["Coming up · 3", "Coming up, 3"],
      ["Your instructions · 1", "Your instructions, 1"],
    ]);
    // Rows carry the shared title/meta projection; other chats' rows never appear.
    for (const text of ["Goal s1", "Quote d1", "scope d1"]) expect(panel!.textContent).toContain(text);
    expect(panel!.textContent).not.toContain("Quote d-elsewhere");

    const open = [...panel!.querySelectorAll("button")].filter((b) => b.textContent === "Open in Work");
    expect(open).toHaveLength(3);
    await click(open[2]); // Your instructions
    const ui = useUI.getState();
    // The thisChat scope rides the VISIT: the saved surface state keeps the user's own
    // filters untouched, so every other Work entry still restores what they left behind.
    expect(ui.surfaceStates.work).toEqual({
      section: "directives", itemId: undefined, scrollTop: 0, filters: { retained: true },
    });
    expect(isThisChatVisit()).toBe(true);
    expect(ui.rightPanel).toBe("work");
    expect(ui.rightCollapsed).toBe(false);
    expect(ui.mobileActive).toBe("work");
    expect(dialog()).toBeNull(); // navigation closes the panel
  });

  it("the Needs you deep link (S7) never scopes — it even ends a live visit scope", async () => {
    beginThisChatVisit();
    openWorkGroup("needs-you");
    const ui = useUI.getState();
    expect(isThisChatVisit()).toBe(false);
    expect(ui.surfaceStates.work).toEqual({
      section: "decisions", itemId: undefined, scrollTop: 0, filters: { retained: true },
    });
  });

  it("routes running/planned groups to the Overview until their group sections register", async () => {
    expect(workSectionForGroup("doing-now")).toBe("overview");
    expect(workSectionForGroup("coming-up")).toBe("overview");
    expect(workSectionForGroup("instructions")).toBe("directives");
    const dispose = registerWorkSection("doing-now", { label: "Doing now", group: "doing-now", render: () => null });
    try {
      expect(workSectionForGroup("doing-now")).toBe("doing-now");
    } finally {
      dispose();
    }
  });

  it("does not reopen by itself when the open chip leaves and later returns", async () => {
    await render(rail([bg("bg-1", "running", SESSION)]));
    await click(chip("doing-now"));
    expect(dialog()).toBeTruthy();
    // The job ends under the reader: chip and panel both go.
    await render(rail([bg("bg-1", "completed", SESSION)]));
    expect(chip("doing-now")).toBeNull();
    expect(dialog()).toBeNull();
    // A new job starts: the chip is back, the panel stays closed until asked for.
    await render(rail([bg("bg-2", "running", SESSION)]));
    expect(chip("doing-now")).toBeTruthy();
    expect(dialog()).toBeNull();
    expect(chip("doing-now")!.getAttribute("aria-expanded")).toBe("false");
  });

  it("traps Tab inside the panel and returns focus to the chip on Escape", async () => {
    await render(rail(MIXED));
    await click(chip("doing-now"));
    const panel = document.querySelector<HTMLElement>(".chat-status-details")!;
    const buttons = [...panel.querySelectorAll<HTMLButtonElement>("button")];
    const first = buttons[0];
    const last = buttons[buttons.length - 1];
    // Real keydowns through the rendered panel, so the test fails if the panel's own
    // onKeyDown trap wiring is removed — not just if the helper's logic breaks.
    for (const [from, to, shiftKey] of [[last, first, false], [first, last, true]] as const) {
      from.focus();
      const tab = new KeyboardEvent("keydown", { key: "Tab", shiftKey, bubbles: true, cancelable: true });
      await act(async () => from.dispatchEvent(tab));
      expect(tab.defaultPrevented).toBe(true);
      expect(document.activeElement).toBe(to);
    }
    await act(async () => panel.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(dialog()).toBeNull();
    expect(chip("doing-now")!.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(chip("doing-now"));
  });
});

it("phone: opens a bottom-sheet Drawer, stacks md actions, and restores focus on close", async () => {
  state.mobile = true;
  await render(rail(MIXED));
  expect(chip("doing-now")!.getAttribute("aria-haspopup")).toBe("dialog");
  await click(chip("doing-now"));
  const sheet = document.querySelector<HTMLElement>(".pl-drawer--bottom");
  expect(sheet).toBeTruthy();
  expect(sheet!.getAttribute("role")).toBe("dialog");
  expect(sheet!.textContent).toContain("Chat status");
  // Phone actions are the md size (the DS default class set, not the sm modifier).
  const open = [...sheet!.querySelectorAll<HTMLButtonElement>("button")]
    .filter((b) => b.textContent === "Open in Work");
  expect(open.length).toBeGreaterThan(0);
  for (const button of open) expect(button.className).not.toContain("pl-btn--sm");
  await click(sheet!.querySelector('[aria-label="Close"]'));
  expect(document.querySelector(".pl-drawer--bottom")).toBeNull();
  expect(chip("doing-now")!.getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(chip("doing-now"));
});

describe("instructions failure (preserved from the ledger strip, scoped to THIS chat)", () => {
  it("names THIS chat's never-loaded ledger even while other chats keep the summed source merely stale", async () => {
    state.ledger = { state: "failed", loadedAt: null, refresh: vi.fn() };
    // The aggregated source: another chat's ledger HAS loaded, so useWorkItems sums this
    // chat's failure down to "stale" — the downgrade that used to hide the failure here.
    await render(rail([bg("bg-run", "running", SESSION)], {}, { status: "stale", error: new Error("down") }));
    const told = chip("instructions")!;
    expect(told.getAttribute("aria-label")).toBe("Told it: couldn't check instructions in this chat. Show details");
    expect(told.querySelector(".chat-status-chip__count")).toBeNull();
    await click(told);
    const panel = dialog()!;
    expect(panel.textContent).toContain("Couldn't check your instructions.");
    // A never-loaded ledger has no knowable count: the heading names the group alone — it
    // never claims "Your instructions · 0" while the chip calls the count unknown.
    const heading = [...panel.querySelectorAll(".chat-status-details__heading")]
      .find((h) => h.textContent!.startsWith("Your instructions"))!;
    expect(heading.textContent).toBe("Your instructions");
    expect(heading.getAttribute("aria-label")).toBe("Your instructions");
    await click([...panel.querySelectorAll("button")].find((b) => b.textContent === "Load again"));
    // Load again refreshes THIS chat's ledger, not the whole cross-chat inventory.
    expect(state.ledger.refresh).toHaveBeenCalledOnce();
    expect(state.sources.directives.retry).not.toHaveBeenCalled();
  });

  it("does not blame this chat for another chat's failure: a healthy local ledger shows no warning", async () => {
    // Another chat's ledger (or the session index) failed: the summed source reads stale/error,
    // but THIS chat's instructions are current — no stale note, no Load again, no problem chip.
    for (const status of ["stale", "error"] as const) {
      await render(rail([directive("d1", SESSION)], {}, { status, error: new Error("elsewhere") }));
      expect(chip("instructions")!.textContent).toBe("Told it 1");
      await click(chip("instructions"));
      const panel = dialog()!;
      expect(panel.textContent).not.toContain("may be out of date");
      expect(panel.textContent).not.toContain("Couldn't check your instructions.");
      expect([...panel.querySelectorAll("button")].some((b) => b.textContent === "Load again")).toBe(false);
      await act(async () => panel.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    }
  });

  it("keeps the retained count on a stale local refresh, offers Load again, and omits empty groups", async () => {
    state.ledger = { state: "stale", loadedAt: 1, refresh: vi.fn() };
    await render(rail([directive("d1", SESSION)], {}, { status: "ready" }));
    expect(chip("instructions")!.textContent).toBe("Told it 1");
    await click(chip("instructions"));
    const panel = dialog()!;
    // The empty running/planned groups render no section at all.
    expect([...panel.querySelectorAll("h2")].map((h) => h.textContent)).toEqual(["Your instructions · 1"]);
    expect(panel.textContent).toContain("Your instructions may be out of date.");
    await click([...panel.querySelectorAll("button")].find((b) => b.textContent === "Load again"));
    expect(state.ledger.refresh).toHaveBeenCalledOnce();
  });
});

describe("live region", () => {
  const tied = directive("d-tied", SESSION, { tiedJob: { kind: "background", id: "bg-tied" } });

  it("is the rail's ONE polite region — a long-lived node, empty until a tied job completes, announcing once", async () => {
    await render(rail([bg("bg-tied", "running", SESSION), tied]));
    const regions = [...host.querySelectorAll("[aria-live], [role='status']")]
      .filter((node) => !node.closest("[aria-hidden='true']"));
    expect(regions).toHaveLength(1);
    const region = regions[0];
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(region.textContent).toBe("");
    expect(chip("instructions")!.textContent).toBe("Told it 1");

    // The job ends: the tied approval leaves the count in the same update, and the whole rail
    // empties — but the announcement is a TEXT CHANGE inside the SAME long-lived node (a
    // region that (re)mounts already holding its text is read by nothing).
    await render(rail([bg("bg-tied", "completed", SESSION), tied]));
    expect(host.querySelector("[aria-live]")).toBe(region);
    expect(region.textContent).toBe("Job bg-tied finished. 1 approval it carried is now fulfilled.");
    expect(host.querySelector('[data-testid="chat-status-rail"]')).toBeNull();

    // …and the announcement does not repeat on later renders.
    await render(rail([bg("bg-tied", "completed", SESSION), tied]));
    expect(host.querySelector("[aria-live]")!.textContent)
      .toBe("Job bg-tied finished. 1 approval it carried is now fulfilled.");
  });

  it("stays silent for a job that carried nothing", async () => {
    await render(rail([bg("bg-plain", "running", SESSION), directive("d1", SESSION)]));
    await render(rail([bg("bg-plain", "completed", SESSION), directive("d1", SESSION)]));
    expect(host.querySelector("[aria-live]")!.textContent).toBe("");
  });

  it("announces BOTH jobs when two tied completions land in one refetch", async () => {
    const tied2 = directive("d-tied2", SESSION, { tiedJob: { kind: "background", id: "bg-tied2" } });
    await render(rail([bg("bg-tied", "running", SESSION), bg("bg-tied2", "running", SESSION), tied, tied2]));
    await render(rail([bg("bg-tied", "completed", SESSION), bg("bg-tied2", "completed", SESSION), tied, tied2]));
    const text = host.querySelector("[aria-live]")!.textContent!;
    expect(text).toContain("Job bg-tied finished. 1 approval it carried is now fulfilled.");
    expect(text).toContain("Job bg-tied2 finished. 1 approval it carried is now fulfilled.");
  });
});

it("people chips keep the mention toggle behavior and render even with zero group chips", async () => {
  const onToggleMention = vi.fn();
  await render(rail([], { cast: ["lead", "scout"], draft: "@lead hello", onToggleMention }));
  // button-scoped: the hidden measure row mirrors the chips as spans with the same class.
  const chips = [...host.querySelectorAll<HTMLButtonElement>("button.chat-roster-chip")];
  expect(chips.map((c) => c.textContent)).toEqual(["@lead", "@scout"]);
  expect(chips[0].getAttribute("aria-pressed")).toBe("true");
  expect(chips[1].getAttribute("aria-pressed")).toBe("false");
  await click(chips[1]);
  expect(onToggleMention).toHaveBeenCalledWith("scout");
  // People chips are plain mention toggles, not dialog triggers.
  expect(chips[0].getAttribute("aria-haspopup")).toBeNull();
});

it("spokenLabel covers the no-goal and uncapped-goal shapes", () => {
  expect(spokenLabel({ id: "doing-now", label: "Doing now", count: 1 }))
    .toBe("Doing now, 1 running in this chat. Show details");
  expect(spokenLabel({ id: "doing-now", label: "Doing now", count: 2, goal: { step: 4, of: null } }))
    .toBe("Doing now, 2 running in this chat, including the goal at step 4. Show details");
});
