// S8d (work-consolidation §10.3–§10.7): the confirm step's editable fields and DIRECT saves —
// each reading goes to its primitive's existing API, in the current chat's context, with no
// second agent turn — plus the shared Edit form's in-place PATCH semantics.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkCreateDialog, WorkEditDialog } from "./WorkCreateDialog";
import { draftFromDirective, draftFromWatch } from "./workPersistence";
import { takeGoalKickoff } from "../chat/chat-store";
import type { WorkInterpretation } from "../lib/api/chat";

const mocks = vi.hoisted(() => ({
  interpretWork: vi.fn(),
  verifiers: vi.fn(async () => ({ types: [], plugin_checks: [{ name: "gh:pr-merged", description: "", plugin_id: "gh" }] })),
  chatMentions: vi.fn(async () => ({ mentions: [{ name: "coder", kind: "acp", description: "", usage: "" }] })),
  createWatch: vi.fn(async (_body: Record<string, unknown>) => ({ ok: true })),
  editWatch: vi.fn(async (_id: string, _body: Record<string, unknown>) => ({ ok: true })),
  addSchedule: vi.fn(async (_body: Record<string, unknown>) => ({ job: { id: "work-j1", prompt: "", schedule: "" } })),
  createDirective: vi.fn(async (_body: Record<string, unknown>) => ({ directive: { id: "d-9", session_id: "chat-1", quote: "q", scope: "s", conditions: "", status: "in_force" } })),
  updateDirective: vi.fn(async (_sid: string, _id: string, _body: Record<string, unknown>) => ({ directive: {} })),
  listDirectives: vi.fn(async (_sid: string) => ({ directives: [] as Record<string, unknown>[], in_force: 0 })),
}));
vi.mock("../lib/api", () => ({ api: mocks, currentSlug: () => "test" }));
vi.mock("../chat/useDirectives", () => ({ directiveStore: () => ({ refresh: async () => null }) }));
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLDivElement;
const onClose = vi.fn();
const onSaved = vi.fn();

const button = (label: string) =>
  Array.from(document.querySelectorAll("button")).find((node) => node.textContent?.trim() === label)!;
const field = (label: string) => Array.from(document.querySelectorAll<HTMLElement>(".pl-field"))
  .find((node) => node.querySelector(".pl-field__label")?.textContent === label)!;
async function setValue(el: Element, value: string) {
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
    : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}

const base = { says: "Reading.", problem: null } as const;
const condition = (over: Partial<NonNullable<NonNullable<WorkInterpretation["followup"]>["condition"]>> = {}): WorkInterpretation => ({
  ...base, kind: "followup", group: "coming",
  followup: { trigger: "condition", time: null, then: "Rebase #874",
    condition: { condition: "PR #868 merges", check: "gh:pr-merged", check_args: { pr: 868 }, interval_s: 1800, repeat: false, expires_in_s: null, ...over } },
});
const time = (repeat: string, label = "Weekdays"): WorkInterpretation => ({
  ...base, kind: "followup", group: "coming",
  followup: { trigger: "time", condition: null, then: "Summarise what merged",
    time: { first_run: "2099-01-05T09:00:00+00:00", repeat, repeat_label: label, timezone: "UTC" } },
});
const job = (who: "agent" | "delegate"): WorkInterpretation =>
  ({ ...base, kind: "job", group: "doing", job: { what: "Re-run the audit", who }, followup: null, instruction: null });
const instruction = (quote: string): WorkInterpretation =>
  ({ ...base, kind: "instruction", group: "told", instruction: { quote, scope: "merges to main", conditions: "" }, followup: null, job: null });

async function confirm(reading: WorkInterpretation, typed = "the words I typed") {
  mocks.interpretWork.mockResolvedValue({ session_id: "chat-1", response: "", messages: [{ role: "system", content: "", interpretation: reading }] });
  await act(async () => root.render(<WorkCreateDialog sessionId="chat-1" onClose={onClose} onSaved={onSaved} />));
  await setValue(document.querySelector("textarea")!, typed);
  await act(async () => button("Continue").click());
}

beforeEach(() => {
  vi.clearAllMocks();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

it("a condition confirm saves ONE watch through the existing API — current chat, plugin check, fixed id — and double-clicking Save can't duplicate it", async () => {
  await confirm(condition());
  expect(document.querySelector(".work-create__dest")?.textContent).toBe("Goes in Coming up as Condition.");
  // The §10.4 editable fields, prefilled from the reading; no cron, no raw verifier JSON.
  expect(field("When").querySelector("input")?.value).toBe("PR #868 merges");
  expect(field("Check with").querySelector("select")?.value).toBe("gh:pr-merged");
  expect(field("Check every").querySelector("input")?.value).toBe("30m");
  expect(field("Then").querySelector("textarea")?.value).toBe("Rebase #874");
  await setValue(field("Stop checking after").querySelector("input")!, "7d");
  const save = button("Save");
  await act(async () => { save.click(); save.click(); }); // a double click is one save
  expect(mocks.createWatch).toHaveBeenCalledTimes(1);
  const body = mocks.createWatch.mock.calls[0][0] as Record<string, unknown>;
  expect(body).toMatchObject({
    condition: "PR #868 merges",
    verifier: { type: "plugin", check: "gh:pr-merged", args: { pr: 868 } },
    interval_s: 1800, repeat: false, run_prompt: "Rebase #874", run_session: "chat-1", trigger: "met",
  });
  expect(body.watch_id).toMatch(/^work-/);
  expect(typeof body.deadline).toBe("number");
  expect(onSaved).toHaveBeenCalledExactlyOnceWith({ group: "coming-up", label: "Coming up", itemId: `watch:${body.watch_id}` });
});

it("a failed save keeps every field and retries with the SAME id; nothing is created twice", async () => {
  mocks.createWatch.mockRejectedValueOnce(new Error("the agent is offline"));
  await confirm(condition());
  await act(async () => button("Save").click());
  expect(document.querySelector(".pl-callout--error")?.textContent).toContain("the agent is offline");
  expect(field("When").querySelector("input")?.value).toBe("PR #868 merges"); // retained
  expect(onSaved).not.toHaveBeenCalled();
  await act(async () => button("Save").click());
  expect(mocks.createWatch).toHaveBeenCalledTimes(2);
  const [first, second] = mocks.createWatch.mock.calls.map((call) => (call[0] as { watch_id: string }).watch_id);
  expect(second).toBe(first);
  expect(onSaved).toHaveBeenCalledTimes(1);
});

it("an instruction records the TYPED bytes — never the model's paraphrase, never trimmed — and shows them read-only", async () => {
  const typed = "  Merge only when the review says PASS\t";
  await confirm(instruction("Merge only when the review says PASS (tidied)"), typed);
  const quote = document.querySelector(".work-create__quote");
  expect(quote?.textContent).toBe(typed);
  expect(field("Your words").querySelector("input, textarea")).toBeNull(); // read-only
  expect(field("Recorded").textContent).toContain("In this chat");
  await act(async () => button("Save").click());
  expect(mocks.createDirective).toHaveBeenCalledExactlyOnceWith({
    session_id: "chat-1", quote: typed, scope: "merges to main", conditions: "",
  });
  expect(onSaved).toHaveBeenCalledExactlyOnceWith({ group: "instructions", label: "Your instructions", itemId: "directive:d-9" });
});

it("an instruction retry re-uses an in-force byte-identical row instead of creating a sibling", async () => {
  const typed = "Merge only when the review says PASS";
  mocks.listDirectives.mockResolvedValue({ in_force: 1, directives: [
    { id: "d-prior", session_id: "chat-1", quote: typed, scope: "merges to main", conditions: "", status: "in_force" },
  ] } as never);
  await confirm(instruction(typed), typed);
  await act(async () => button("Save").click());
  expect(mocks.createDirective).not.toHaveBeenCalled();
  expect(onSaved).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ itemId: "directive:d-prior" }));
});

it("a time confirm shows human Repeat choices — never cron — and saves through the scheduler with the chat as owner", async () => {
  await confirm(time("0 9 * * 1-5"));
  expect(document.querySelector(".work-create__dest")?.textContent).toBe("Goes in Coming up as Time.");
  const repeat = field("Repeat").querySelector("select")!;
  expect(repeat.value).toBe("weekdays");
  expect(Array.from(repeat.options).map((option) => option.textContent)).toEqual([
    "Once", "Every 30 min", "Hourly", "Daily", "Weekdays", "Weekly",
  ]);
  expect(document.querySelector(".work-create__body")?.textContent).not.toContain("* *");
  await act(async () => button("Save").click());
  expect(mocks.addSchedule).toHaveBeenCalledExactlyOnceWith({
    prompt: "Summarise what merged", schedule: "0 9 * * 1-5", job_id: expect.stringMatching(/^work-/),
    origin_session: "chat-1", timezone: "UTC",
  });
  expect(onSaved).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ itemId: "schedule:work-j1" }));
});

it("an unmapped repeat shows a sentence instead of the select; the words — not cron — are the way to change it", async () => {
  await confirm(time("17 3 2 * *", "monthly on the 2nd"));
  const repeat = field("Repeat");
  expect(repeat.querySelector("select")).toBeNull();
  expect(repeat.textContent).toContain("Custom schedule");
  expect(repeat.textContent).toContain("← Change the words");
  expect(document.body.textContent).not.toContain("17 3 2 * *");
  await act(async () => button("Save").click());
  expect((mocks.addSchedule.mock.calls[0][0] as { schedule: string }).schedule).toBe("17 3 2 * *");
});

it("a job Starts as one normal (visible) current-chat turn; a chosen delegate rides the @-mention dispatch", async () => {
  await confirm(job("agent"));
  expect(field("Starts").textContent).toContain("Now");
  await act(async () => button("Start").click());
  expect(takeGoalKickoff("chat-1")).toEqual({ prompt: "Start: Re-run the audit", hidden: false });
  expect(onSaved).toHaveBeenCalledExactlyOnceWith({ group: "doing-now", label: "Doing now" });

  onSaved.mockClear();
  await act(async () => root.unmount());
  root = createRoot(host);
  await confirm(job("delegate"));
  expect(button("Start").disabled).toBe(true); // no delegate picked yet → incomplete
  await setValue(field("Who").querySelector("select")!, "coder");
  await act(async () => button("Start").click());
  expect(takeGoalKickoff("chat-1")).toEqual({ prompt: "@coder Start: Re-run the audit", hidden: false });
});

it("a problem reading has no Save path at all", async () => {
  await confirm({ ...base, says: "x", kind: "followup", group: "coming", followup: null, problem: "Scheduling is unavailable." });
  expect(document.querySelector(".pl-callout--warning")?.textContent).toContain("Scheduling is unavailable.");
  expect(Array.from(document.querySelectorAll("button")).map((node) => node.textContent?.trim()))
    .not.toContain("Save");
});

it("Edit follow-up PATCHes the original watch id in place and Cancel saves nothing", async () => {
  const watch = { id: "w-874", condition: "merge-watch-874", status: "active", repeat: false,
    verifier: { type: "plugin", check: "gh:pr-merged" }, run_prompt: "rebase", check_count: 41 };
  await act(async () => root.render(
    <WorkEditDialog draft={draftFromWatch(watch)} target={{ kind: "condition", id: "w-874" }} onClose={onClose} onSaved={onSaved} />,
  ));
  expect(document.querySelector(".pl-dialog__title")?.textContent).toBe("Edit follow-up");
  await act(async () => button("Cancel").click());
  expect(onClose).toHaveBeenCalled();
  expect(mocks.editWatch).not.toHaveBeenCalled(); // Cancel saves nothing

  await act(async () => root.render(
    <WorkEditDialog draft={draftFromWatch(watch)} target={{ kind: "condition", id: "w-874" }} onClose={onClose} onSaved={onSaved} />,
  ));
  await setValue(field("Then").querySelector("textarea")!, "rebase and ping");
  await act(async () => button("Save changes").click());
  // Only the changed key, on the SAME id — history/check count live on the preserved row.
  expect(mocks.editWatch).toHaveBeenCalledExactlyOnceWith("w-874", { run_prompt: "rebase and ping" });
  expect(mocks.createWatch).not.toHaveBeenCalled();
});

it("Edit instruction keeps the quote immutable and PATCHes scope/conditions only", async () => {
  const row = { id: "d-1", session_id: "chat-1", quote: "exact words", scope: "deploys", conditions: "until Friday",
    status: "in_force", created_at: "", closed_by: null, closed_at: null, close_reason: null } as const;
  await act(async () => root.render(
    <WorkEditDialog draft={draftFromDirective(row)} target={{ kind: "instruction", id: "d-1", sessionId: "chat-1" }} onClose={onClose} onSaved={onSaved} />,
  ));
  expect(document.querySelector(".pl-dialog__title")?.textContent).toBe("Edit instruction");
  const words = field("Your words");
  expect(words.querySelector(".work-create__quote")?.textContent).toBe("exact words");
  expect(words.querySelector("input, textarea")).toBeNull();
  expect(words.textContent).toContain("withdraw this and add a new one");
  await setValue(field("What it covers").querySelector("input")!, "deploys and releases");
  await act(async () => button("Save changes").click());
  expect(mocks.updateDirective).toHaveBeenCalledExactlyOnceWith("chat-1", "d-1", {
    scope: "deploys and releases", conditions: "until Friday",
  });
});
