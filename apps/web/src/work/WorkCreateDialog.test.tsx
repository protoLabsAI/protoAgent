import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkCreateDialog, type WorkCreateHandoff } from "./WorkCreateDialog";
import type { WorkInterpretation } from "../lib/api/chat";

const interpretWork = vi.hoisted(() => vi.fn());
// Step 2's field pickers read the live registries lazily; empty lists keep the current
// values as the only offers, which is all these step-1/step-2 presentation tests need.
vi.mock("../lib/api", () => ({ api: {
  interpretWork,
  verifiers: vi.fn(async () => ({ types: [], plugin_checks: [] })),
  chatMentions: vi.fn(async () => ({ mentions: [] })),
} }));
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLDivElement;
let opener: HTMLButtonElement;
const onClose = vi.fn();
const onReady = vi.fn();

const dialog = () => document.querySelector<HTMLElement>('[role="dialog"]');
const textarea = () => document.querySelector<HTMLTextAreaElement>("textarea")!;
const button = (label: string) =>
  Array.from(document.querySelectorAll("button")).find((node) => node.textContent?.trim() === label)!;
const reading = (instruction: WorkInterpretation["instruction"] = null): WorkInterpretation => ({
  says: "I'll check #874 every 30 min.",
  kind: instruction ? "instruction" : "followup",
  group: instruction ? "told" : "coming",
  followup: instruction ? null : { trigger: "condition", condition: { condition: "PR #868 merges", check: "pr", check_args: {}, interval_s: 1800, repeat: false, expires_in_s: null }, then: "Rebase" },
  instruction,
  problem: null,
});

// A current chat by default — the no-chat case opts out with an explicit undefined.
async function render(props: Partial<Parameters<typeof WorkCreateDialog>[0]> = {}) {
  await act(async () => root.render(<WorkCreateDialog onClose={onClose} sessionId="chat-1" {...props} />));
}
async function type(value: string) {
  const box = textarea();
  const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  await act(async () => {
    set.call(box, value);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  host = document.createElement("div");
  opener = document.createElement("button");
  document.body.append(host, opener);
  opener.focus();
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  opener.remove();
  vi.unstubAllGlobals();
});

it("step 1 shows the approved label, hint and example chips; a chip fills the box; Continue enables only with text", async () => {
  await render();
  expect(document.querySelector(".pl-dialog__title")?.textContent).toBe("New");
  expect(dialog()?.getAttribute("aria-modal")).toBe("true");
  expect(document.querySelector(".pl-field__label")?.textContent).toBe("What should the agent do, and when?");
  expect(document.querySelector(".pl-field__hint")?.textContent)
    .toBe("Say it in your own words. You'll see how the agent reads it before anything is saved.");
  expect(document.activeElement).toBe(textarea());
  // The §10.2 examples, verbatim — they fill the box, they never pick a kind.
  const chips = Array.from(document.querySelectorAll("button.pl-chip .pl-chip-label")).map((node) => node.textContent);
  expect(chips).toEqual([
    "Check #874 every 30 min and rebase it when #868 merges",
    "Every weekday at 9, summarise what merged overnight",
    "Merge only when the review says PASS",
    "Re-run the design-system audit on protoContent",
  ]);
  expect(button("Continue").disabled).toBe(true);
  await act(async () => button("Check #874 every 30 min and rebase it when #868 merges").click());
  expect(textarea().value).toBe("Check #874 every 30 min and rebase it when #868 merges");
  expect(document.activeElement).toBe(textarea());
  expect(button("Continue").disabled).toBe(false);
  await act(async () => button("Cancel").click());
  expect(onClose).toHaveBeenCalled();
  expect(interpretWork).not.toHaveBeenCalled();
});

it("Continue sends one marked interpret turn to the current chat, pends read-only, and hands off the typed bytes", async () => {
  let resolve!: (value: unknown) => void;
  interpretWork.mockReturnValue(new Promise((r) => { resolve = r; }));
  await render({ sessionId: "chat-1", group: "coming-up", onReady });
  // The group "+" seeds only the placeholder — nothing but the words and the chat go out.
  expect(textarea().placeholder).toBe("What should happen, and when — or on what condition?");
  await type("Watch the thing");
  await act(async () => button("Continue").click());
  expect(interpretWork).toHaveBeenCalledExactlyOnceWith("Watch the thing", "chat-1");
  expect(textarea().readOnly).toBe(true);
  const status = document.querySelector('[role="status"].work-create__status');
  expect(status?.textContent).toContain("Reading what you wrote…");
  expect(status?.querySelector(".pl-spinner")).toBeTruthy();
  expect(button("Continue").disabled).toBe(true);
  // A model slip can't change the operator's words: the console overwrites the quote.
  await act(async () => resolve({ session_id: "chat-1", response: "", messages: [
    { role: "system", content: "", interpretation: reading({ quote: "REWRITTEN", scope: "merges", conditions: "" }) },
  ] }));
  const handoff = onReady.mock.calls[0][0] as WorkCreateHandoff;
  expect(handoff.typed).toBe("Watch the thing");
  expect(handoff.sessionId).toBe("chat-1");
  expect(handoff.interpretation.instruction?.quote).toBe("Watch the thing");
  // Step 2 isn't built yet (S8d): the reading shows where it lands, with no Start/Save path.
  expect(document.querySelector(".pl-dialog__title")?.textContent).toBe("Check this before saving");
  expect(document.querySelector(".pl-callout__body")?.textContent).toBe("I'll check #874 every 30 min.");
  expect(document.querySelector(".work-create__dest")?.textContent).toBe("Goes in Your instructions as Instruction.");
  expect(Array.from(document.querySelectorAll("button")).map((node) => node.textContent?.trim()))
    .not.toContain("Start");
  // ← Change the words returns to step 1 with the text kept.
  await act(async () => button("← Change the words").click());
  expect(textarea().value).toBe("Watch the thing");
  expect(textarea().readOnly).toBe(false);
});

it("a failed read shows a readable error and retry keeps the words", async () => {
  interpretWork.mockRejectedValueOnce(new Error("The agent is offline."));
  interpretWork.mockResolvedValueOnce({ session_id: "s", response: JSON.stringify(reading()), messages: [] });
  await render({ onReady });
  await type("Watch the thing");
  await act(async () => button("Continue").click());
  expect(document.querySelector(".pl-callout--error")?.textContent)
    .toContain("The agent is offline. Your words are kept — Continue tries again.");
  expect(textarea().readOnly).toBe(false);
  expect(textarea().value).toBe("Watch the thing");
  await act(async () => button("Continue").click());
  // The flattened `response` fallback parses too, and a condition reads as Coming up.
  expect((onReady.mock.calls[0][0] as WorkCreateHandoff).interpretation.kind).toBe("followup");
  expect(document.querySelector(".work-create__dest")?.textContent).toBe("Goes in Coming up as Condition.");
});

it("without a current chat nothing is ever posted: Continue stays gated and the dialog says why", async () => {
  // POSTing an empty session_id would have /api/chat mint a session and the marked turn
  // land in a hidden new chat — the dialog must gate, never fall back to a minted id.
  await render({ sessionId: undefined, onReady });
  expect(document.querySelector(".work-create__body .pl-callout--warning")?.textContent)
    .toContain("No chat is open");
  await type("Watch the thing");
  expect(button("Continue").disabled).toBe(true);
  await act(async () => button("Continue").click());
  expect(interpretWork).not.toHaveBeenCalled();
  expect(onReady).not.toHaveBeenCalled();
});

it("Escape closes through the overlay stack and focus returns to the opener", async () => {
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { cb(0); return 0; });
  await render();
  expect(document.activeElement).toBe(textarea());
  await act(async () => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  expect(onClose).toHaveBeenCalled();
  await act(async () => root.render(null));
  expect(document.activeElement).toBe(opener);
});

it("at a phone viewport the flow is the bottom sheet with the primary stacked first, never a centered dialog", async () => {
  const mql = { matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() };
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue(mql));
  await render();
  expect(document.querySelector(".pl-dialog")).toBeNull();
  const sheet = document.querySelector<HTMLElement>(".pl-drawer.work-create--sheet")!;
  expect(sheet.classList.contains("pl-drawer--bottom")).toBe(true);
  // Primary on top: the sheet's DOM order puts Continue before Cancel (§10.7).
  const foot = Array.from(sheet.querySelectorAll<HTMLButtonElement>(".pl-drawer__foot .pl-btn"));
  expect(foot.map((node) => node.textContent?.trim())).toEqual(["Continue", "Cancel"]);
  // md buttons (the DS default carries no size class) — never the sm/xs desktop density.
  expect(foot.some((node) => /pl-btn--(sm|xs)/.test(node.className))).toBe(false);
});
