// DS audit card 4: the raw `<textarea className="workflow-gate-edit">` in both gate
// editors (PendingGateCard here, InlineGate in RunTimeline) became DS `Textarea`
// (@protolabsai/ui/forms). This exercises PendingGateCard end to end — Edit reveals the
// DS Textarea, still keyed by aria-label "edited prompt" and still styled by
// workflow-gate-edit (workflows.css:282) — and source-guards RunTimeline, whose gate
// lives behind a useQuery poll that's costly to stand up in jsdom.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkflowPausedRun } from "../lib/types";
import { PendingGateCard } from "./WorkflowsSurface";
import workflowsSrc from "./WorkflowsSurface.tsx?raw";
import runTimelineSrc from "./RunTimeline.tsx?raw";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const RUN: WorkflowPausedRun = {
  run_id: "r1",
  recipe_name: "nightly",
  paused_step: "review",
  prompt: "Approve the plan?",
  step_outputs: {},
  inputs: {},
};

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

const btn = (text: string) =>
  Array.from(container.querySelectorAll("button")).find((b) => (b.textContent ?? "").trim() === text);
const textarea = () => container.querySelector<HTMLTextAreaElement>('textarea[aria-label="edited prompt"]');

function render(onEdit: (prompt: string) => void = () => {}) {
  act(() =>
    root.render(
      h(PendingGateCard, { run: RUN, busy: false, onApprove: () => {}, onReject: () => {}, onEdit }),
    ),
  );
}

// React controlled <textarea> onChange fires on a native `input` event; go through the
// prototype value setter so React sees the mutation.
function typeInto(el: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("PendingGateCard gate editor → DS Textarea", () => {
  it("Edit reveals a DS Textarea named 'edited prompt', styled by workflow-gate-edit, prefilled + rows=6", () => {
    render();
    expect(textarea()).toBeNull(); // read-only <pre> until Edit
    act(() => btn("Edit")!.click());

    const ta = textarea();
    expect(ta).toBeTruthy();
    expect(ta!.classList.contains("workflow-gate-edit")).toBe(true); // workflows.css:282 still styles it
    expect(ta!.classList.contains("pl-textarea")).toBe(true); // it's the DS component now
    expect(ta!.rows).toBe(6);
    expect(ta!.value).toBe(RUN.prompt);
  });

  it("edits flow through onChange to Save & run", () => {
    const onEdit = vi.fn();
    render(onEdit);
    act(() => btn("Edit")!.click());
    typeInto(textarea()!, "Approve with edits");
    act(() => btn("Save & run")!.click());
    expect(onEdit).toHaveBeenCalledWith("Approve with edits");
  });

  it("neither gate file keeps a raw <textarea> — both use the DS Textarea", () => {
    for (const src of [workflowsSrc, runTimelineSrc]) {
      expect(src).not.toContain("<textarea");
      expect(src).toContain("<Textarea");
      expect(src).toContain('className="workflow-gate-edit"');
      expect(src).toContain('aria-label="edited prompt"');
    }
  });
});
