import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { HitlDisplayTable, HitlPayload } from "../lib/types";
import { HitlForm } from "./HitlForm";

// Keep prose synchronous; the form and design-system controls/table are real.
vi.mock("./LazyMarkdown", () => ({ Markdown: ({ children }: { children: string }) => h("p", null, children) }));
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const hostile = '<img src=x onerror="alert(1)"><a href="https://example.com">Go</a><script>alert(1)</script>';
const display: HitlDisplayTable = {
  component: "table",
  props: { columns: ["Name", "Value"], rows: [[hostile, 0], [true, false], [null, "https://example.com"]] },
};
const approval: HitlPayload = { kind: "approval", description: "Review these values", detail: "echo <literal>", display };
let container: HTMLDivElement;
let root: Root;
let onSubmit: ReturnType<typeof vi.fn<(response: Record<string, unknown> | string) => void>>;
let onCancel: ReturnType<typeof vi.fn<() => void>>;
let onApproveAlways: ReturnType<typeof vi.fn<() => void>>;
const render = (payload = approval, busy = false) => act(() => root.render(h(HitlForm, {
  payload, busy, onSubmit, onCancel, onApproveAlways,
})));
const button = (label: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === label)!;
const click = (element: HTMLElement) => act(() => element.click());
const key = (element: Element, value: string) => act(() => element.dispatchEvent(new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true })));

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  onSubmit = vi.fn();
  onCancel = vi.fn();
  onApproveAlways = vi.fn();
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("HITL inert display", () => {
  it("renders semantic DS markup, scalar text, escaped hostile content, and retained prose", () => {
    render();
    expect(container.querySelector("table.pl-table")).not.toBeNull();
    expect([...container.querySelectorAll("th")].map((c) => c.textContent)).toEqual(["Name", "Value"]);
    expect([...container.querySelectorAll("td")].map((c) => c.textContent)).toEqual([hostile, "0", "true", "false", "", "https://example.com"]);
    expect(container.querySelector(".hitl-display img, .hitl-display a, .hitl-display script, iframe")).toBeNull();
    expect(container.textContent).toContain("Review these values");
    expect(container.querySelector("pre")?.textContent).toBe("echo <literal>");
  });

  it("display clicks and ordinary keys have no authority; Escape retains host dismissal", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    render();
    const table = container.querySelector("table")!;
    expect(table.querySelector("button, input, a, [tabindex], [contenteditable]")).toBeNull();
    for (const element of [table, ...table.querySelectorAll("th, td")]) {
      click(element as HTMLElement);
      for (const value of ["Enter", " ", "ArrowDown", "a"]) key(element, value);
    }
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
    expect(onApproveAlways).not.toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
    key(table, "Escape");
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("preserves exact host option values, focus, and busy disabling", () => {
    const options = [{ label: "Allow read", value: '  allow:read/一 "yes"  ', primary: true }, { label: "Reject", value: "reject_once" }];
    render({ ...approval, options });
    expect(document.activeElement).toBe(button("Allow read"));
    expect(container.querySelectorAll("button")).toHaveLength(2);
    options.forEach((option) => click(button(option.label)));
    expect(onSubmit.mock.calls).toEqual(options.map((option) => [option.value]));
    onSubmit.mockClear();
    render({ ...approval, options }, true);
    options.forEach((option) => {
      expect(button(option.label).disabled).toBe(true);
      click(button(option.label));
    });
    key(container.querySelector("table")!, "Escape");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("preserves legacy Approve/Deny/always, session restriction, busy and focus restoration", () => {
    const previous = document.createElement("button");
    document.body.appendChild(previous);
    previous.focus();
    render();
    expect(document.activeElement).toBe(button("Deny"));
    click(button("Approve"));
    click(button("Deny"));
    click(button("Approve & don't ask again"));
    expect(onSubmit.mock.calls).toEqual([["approved"], ["denied"]]);
    expect(onApproveAlways).toHaveBeenCalledTimes(1);
    render(approval, true);
    for (const b of container.querySelectorAll("button")) { expect(b.disabled).toBe(true); click(b); }
    expect(onSubmit).toHaveBeenCalledTimes(2);
    expect(onApproveAlways).toHaveBeenCalledTimes(1);
    render({ ...approval, session_allow: false });
    expect(button("Approve & don't ask again")).toBeUndefined();
    act(() => root.render(null));
    expect(document.activeElement).toBe(previous);
    previous.remove();
  });

  const payloads: HitlPayload[] = [
    approval,
    { kind: "form", description: "Form prose", steps: [{ schema: { properties: { name: { type: "string", default: "Ada" } } } }] },
    { question: "Question prose" },
  ];
  it.each(payloads)("renders display alongside every supported prompt: $kind $question", (payload) => {
    render({ ...payload, display });
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.textContent).toContain(payload.description || payload.question);
  });

  const invalid: unknown[] = [undefined, null, "table", [], {}, { component: "plugin:interactive", props: display.props },
    { component: "table", props: null }, { component: "table", props: { columns: "bad", rows: [] } },
    ...[
      { columns: [42], rows: [] }, { columns: ["x"], rows: "bad" }, { columns: ["x"], rows: [null] },
      { columns: ["x"], rows: [[]] }, { columns: ["x"], rows: [[{}]] }, { columns: ["x"], rows: [[[1]]] },
      { columns: ["x"], rows: [[NaN]] }, { columns: ["x"], rows: [[Infinity]] },
      { columns: ["x"], rows: [[undefined]] }, { columns: ["x".repeat(201)], rows: [] },
      { columns: ["x"], rows: [["x".repeat(1001)]] }, { columns: Array(17).fill("x"), rows: [] },
      { columns: ["x"], rows: Array(101).fill([1]) },
      { columns: ["x"], rows: Array(100).fill(["一".repeat(1000)]) },
    ].map((props) => ({ component: "table", props })),
  ];
  it.each(invalid.map((value, i) => [i, value] as const))("ignores invalid display %s and keeps approval/form/question usable", (_i, value) => {
    for (const payload of payloads) {
      act(() => root.render(null));
      render({ ...payload, display: value as HitlDisplayTable });
      expect(container.querySelector("table, iframe")).toBeNull();
      expect(container.textContent).toContain(payload.description || payload.question);
      if (payload.kind === "approval") {
        expect(container.querySelector("pre")?.textContent).toBe(approval.detail);
        click(button("Approve"));
        expect(onSubmit).toHaveBeenLastCalledWith("approved");
      } else if (payload.steps) {
        click(button("Submit"));
        expect(onSubmit).toHaveBeenLastCalledWith({ name: "Ada" });
      } else {
        expect(container.querySelector("textarea")).not.toBeNull();
        click(button("Dismiss"));
        expect(onCancel).toHaveBeenCalled();
      }
    }
  });

  it("accepts boundary dimensions and Unicode lengths while ignoring executable extra props", () => {
    const columns = Array(16).fill("😀".repeat(200));
    const rows = Array.from({ length: 100 }, () => Array(16).fill(null));
    rows[0][0] = "😀".repeat(1000);
    render({ ...approval, display: { component: "table", props: { columns, rows, onClick: onSubmit, dangerouslySetInnerHTML: { __html: hostile } } } as HitlDisplayTable });
    expect(container.querySelectorAll("td")).toHaveLength(1600);
    click(container.querySelector("td")!);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(container.querySelector("img")).toBeNull();
  });

  it("enforces the whole-table byte ceiling including JSON separator spaces", () => {
    const rows = Array.from({ length: 66 }, () => ["x".repeat(980)]);
    const candidate: HitlDisplayTable = { component: "table", props: { columns: ["x"], rows } };
    const size = new TextEncoder().encode(JSON.stringify(candidate)).length + 6 + rows.length - 1;
    rows.push(["x".repeat(65536 - size - 6)]); // an additional row adds six separator/quote bytes
    render({ ...approval, display: candidate });
    expect(container.querySelector("table")).not.toBeNull();
    rows[rows.length - 1][0] += "x";
    render({ ...approval, display: candidate });
    expect(container.querySelector("table")).toBeNull();
    expect(button("Approve").disabled).toBe(false);
  });
});
