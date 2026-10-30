import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { EvalCaseEditor } from "./EvalCaseEditor";
import type { EvalCase } from "../lib/api/evalCases";

const draft: EvalCase = { id: "case-1", name: "Promoted turn", category: "instance", kind: "ask", prompt: "Find sources", source_trace_id: "trace-1", tier: "gate", comment: "Needs sources" };
let root: Root;
let el: HTMLDivElement;
let saved: EvalCase[];
const close = vi.fn();
const source = { session_id: "session-1", trace_id: "trace-1" };
let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  saved = []; close.mockReset();
  fetchMock = vi.fn(async (url: string, init: RequestInit) => {
    if (url.endsWith("/draft")) {
      expect(JSON.parse(init.body as string)).toEqual(source);
      return new Response(JSON.stringify(draft));
    }
    const value = JSON.parse(init.body as string).case as EvalCase;
    saved.push(value);
    return value.expected_patterns?.length || value.expected_tools
      ? new Response(JSON.stringify({ ok: true, case: value }), { status: 201 })
      : new Response(JSON.stringify({ detail: "Case must assert something" }), { status: 422 });
  });
  vi.stubGlobal("fetch", fetchMock);
  el = document.createElement("div"); document.body.append(el); root = createRoot(el);
});
afterEach(() => { act(() => root.unmount()); el.remove(); vi.unstubAllGlobals(); });
async function mount() { await act(async () => root.render(<EvalCaseEditor source={source} onClose={close} />)); }
async function click(text: string) { await act(async () => [...el.querySelectorAll("button")].find((b) => b.textContent === text)!.click()); }
async function fill(label: string, value: string) {
  const field = [...el.querySelectorAll("label")].find((node) => node.firstChild?.textContent === label)!.querySelector("textarea")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
it("shows the server refusal without inventing a no-tools assertion, then saves edits", async () => {
  await mount();
  expect(saved).toEqual([]);
  await click("Save");
  expect(el.querySelector('[role="alert"]')?.textContent).toBe("Case must assert something");
  expect(saved[0]).not.toHaveProperty("expected_tools");
  expect(close).not.toHaveBeenCalled();
  await fill("Expected reply patterns", "source.*example\nhttps://example.com");
  await fill("Prompt", "Find reliable sources");
  await click("Save");
  expect(saved[1]).toEqual(expect.objectContaining({ ...draft, prompt: "Find reliable sources", expected_patterns: ["source.*example", "https://example.com"] }));
  expect(close).toHaveBeenCalledOnce();
});
it("cancels an edited draft without saving", async () => {
  await mount(); await fill("Expected tools", "search"); await click("Cancel");
  expect(close).toHaveBeenCalledOnce(); expect(saved).toEqual([]);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
it("preserves an explicit no-tools assertion", async () => {
  await mount();
  await act(async () => el.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  await click("Save");
  expect(saved[0].expected_tools).toEqual([]);
  expect(close).toHaveBeenCalledOnce();
});
it("shows draft failures and allows cancel without a save", async () => {
  fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "The turn must be rated or carry friction" }), { status: 409 }));
  await mount();
  expect(el.querySelector('[role="alert"]')?.textContent).toContain("rated or carry friction");
  await click("Cancel"); expect(saved).toEqual([]);
});
