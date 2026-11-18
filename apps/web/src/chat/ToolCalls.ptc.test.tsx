import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it } from "vitest";
import type { ToolCall } from "../lib/types";
import { ToolCalls } from "./ToolCalls";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLElement;
let root: Root;
const parent: ToolCall = { id: "script", name: "execute_code", status: "running", input: "print('hello')" };
const child = (n: number, overrides: Partial<ToolCall> = {}): ToolCall => ({
  id: `c${n}`, parentId: parent.id, name: "ptc:web_search", status: "done",
  input: JSON.stringify({ limit: 2, query: `query ${n}` }), output: "result", ...overrides,
});
function render(calls: ToolCall[]) {
  act(() => root.render(<ToolCalls calls={calls} flat />));
}
function click(selector: string) {
  act(() => container.querySelector<HTMLButtonElement>(selector)!.click());
}
const header = () => container.querySelector(".pl-toolcard__head")!;
const names = (el: Element) => [...el.querySelectorAll(".pl-toolcard__name")].map((node) => node.textContent);
beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

it("nests script labels between input and stdout in start order with accessible disclosures", () => {
  render([{ ...parent, status: "done", output: "hello" }, child(1), child(2), child(3)]);
  expect(header().getAttribute("aria-expanded")).toBe("false");
  expect(container.querySelector('[role="group"]')).toBeNull();
  click(".pl-toolcard__head");
  const body = container.querySelector(".pl-toolcard__body")!;
  const group = body.querySelector('[role="group"]')!;
  expect(group.getAttribute("aria-label")).toBe("calls from script (3)");
  expect([...body.children].map((el) => el.querySelector(".pl-toolcard__label")?.textContent))
    .toEqual(["input", "calls from script (3)", "result"]);
  expect(names(group)).toEqual(['tools.web_search "query 1"', 'tools.web_search "query 2"', 'tools.web_search "query 3"']);
  expect(group.querySelector(".tool-manage-btn")).toBeNull();
  expect(container.querySelector(".tool-manage-btn")?.getAttribute("aria-label")).toBe("Manage execute_code in Tools settings");
  for (const button of group.querySelectorAll<HTMLButtonElement>(".pl-toolcard__head")) {
    expect(button.disabled).toBe(false);
    button.focus();
    expect(document.activeElement).toBe(button);
  }
});

it.each([false, true])("preserves plain execute_code headers and silence through settle (ordinary children: %s)", (withChildren) => {
  const kids = withChildren ? [child(1, { name: "web_search" })] : [];
  for (const status of ["running", "done", "error"] as const) {
    render([{ ...parent, status }, ...kids]);
    expect(header().querySelector(".pl-toolcard__name")?.textContent)
      .toBe(withChildren ? "execute_code · 1 tool" : "execute_code");
    expect(container.querySelector('[aria-live="polite"]')).toBeNull();
  }
});

it("updates header counts, announces only the settle, and distinguishes blocked and unfinished calls", () => {
  const kids = [child(1, { status: "running", input: undefined, output: undefined }), child(2, { status: "error" }),
    child(3, { output: "Blocked by policy: denied" })];
  render([parent, ...kids]);
  expect(header().textContent).toContain("3 calls · 1 running");
  expect(container.querySelector('[aria-live="polite"]')?.textContent).toBe("");
  const settled = { ...parent, status: "done" as const };
  render([settled, ...kids]);
  expect(header().textContent).toContain("3 calls · 1 failed · 1 blocked");
  const announcement = container.querySelector('[aria-live="polite"]')!.textContent;
  expect(announcement).toBe("Script finished: 3 calls · 1 failed · 1 blocked");
  click(".pl-toolcard__head");
  const group = container.querySelector('[role="group"]')!;
  const stopped = group.querySelector(".tool-stopped")!;
  expect(stopped.textContent).toContain("stopped");
  // The existing stopped style suppresses the DS success glyph.
  expect(getComputedStyle(stopped.querySelector(".pl-toolcard__status")!).display).toBe("none");
  expect(stopped.querySelector<HTMLButtonElement>(".pl-toolcard__head")!.disabled).toBe(false);
  const blocked = [...group.querySelectorAll(".pl-toolcard")].find((el) => el.textContent?.includes("blocked by policy"))!;
  expect(blocked.querySelector(".pl-toolcard__status--error")).not.toBeNull();
  render([settled, child(1), ...kids.slice(1)]);
  expect(container.querySelector('[aria-live="polite"]')!.textContent).toBe(announcement);
  expect(container.querySelectorAll('[aria-live="polite"]')).toHaveLength(1);
  expect(container.querySelector(".tool-stopped")).toBeNull();
});

it("folds finished successes above eight calls while failures, denials and running calls retain start order", () => {
  const kids = Array.from({ length: 12 }, (_, i) => child(i, i === 2 || i === 7 ? { status: "error" }
    : i === 5 ? { output: "Blocked by policy: denied" } : {}));
  render([{ ...parent, status: "done" }, ...kids]);
  click(".pl-toolcard__head");
  const group = container.querySelector('[role="group"]')!;
  expect(group.querySelectorAll(".pl-toolcard-summary")).toHaveLength(1);
  expect(group.querySelector(".pl-toolcard-summary__text")?.textContent).toBe("9 successful calls");
  expect(names(group)).toEqual(['tools.web_search "query 2"', 'tools.web_search "query 5" · blocked by policy', 'tools.web_search "query 7"']);
  click(".pl-toolcard-summary__head");
  expect(names(group.querySelector(".pl-toolcard-summary")!)).toEqual(
    [0, 1, 3, 4, 6, 8, 9, 10, 11].map((i) => `tools.web_search "query ${i}"`),
  );
  render([parent, ...kids.map((kid, i) => i === 1 || i === 10 ? { ...kid, status: "running" as const } : kid)]);
  expect([...group.children].filter((el) => el.classList.contains("pl-toolcard")).map((el) => names(el)[0]))
    .toEqual(['tools.web_search "query 1"', 'tools.web_search "query 2"', 'tools.web_search "query 5" · blocked by policy', 'tools.web_search "query 7"', 'tools.web_search "query 10"']);
});

it("preserves task children and non-bridged execute_code children", () => {
  for (const name of ["task", "execute_code"]) {
    render([{ ...parent, name, input: '{"subagent_type":"researcher"}', output: "report", status: "done" }, child(1, { name: "web_search" })]);
    if (header().getAttribute("aria-expanded") === "false") click(".pl-toolcard__head");
    expect(container.querySelector('[role="group"]')).toBeNull();
    expect(container.querySelector(".pl-toolcard__children .pl-toolcard__name")?.textContent).toBe("web_search");
    expect(container.querySelector(".pl-toolcard__body")!.lastElementChild?.className).toBe("pl-toolcard__children");
    if (name === "task") expect(header().textContent).toContain("task → researcher · 1 tool");
  }
});

it("announces once when the same script appears in a timeline and spotlight", () => {
  const draw = (call: ToolCall) => act(() => root.render(<>
    <ToolCalls calls={[call, child(1)]} flat />
    <ToolCalls calls={[call, child(1)]} spotlight />
  </>));
  draw(parent);
  draw({ ...parent, status: "done" });
  expect([...container.querySelectorAll('[aria-live="polite"]')].filter((el) => el.textContent)).toHaveLength(1);
});
