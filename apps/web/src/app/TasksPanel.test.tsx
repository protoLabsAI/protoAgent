// TaskCreateDialog is a content dialog reused by the Tasks panel and the Work-overview quick-add.
// DS 0.63 card 3a (#3688) opts every content dialog into `padding="roomy"` so its body keeps its
// 24px padding once the app-wide `.pl-dialog__body` theme.css rule is deleted. Guard that the
// rendered dialog body carries the DS `--roomy` modifier. createRoot/act + the real DS components,
// like UtilityWidget.test.tsx — no testing-library dep.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { TaskCreateDialog } from "./TasksPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
});

describe("TaskCreateDialog — DS 0.63 roomy dialog body (#3688)", () => {
  it("renders its dialog body with the roomy padding modifier", () => {
    act(() =>
      root.render(
        h(TaskCreateDialog, {
          open: true,
          onClose: () => {},
          onCreate: () => {},
          busy: false,
        }),
      ),
    );
    // The DS Dialog portals its card to <body>; `padding="roomy"` adds the `--roomy` modifier
    // alongside the base `.pl-dialog__body`.
    const body = document.querySelector(".pl-dialog__body");
    expect(body).not.toBeNull();
    expect(body!.classList.contains("pl-dialog__body--roomy")).toBe(true);
    // The task form still lives inside that roomy body.
    expect(body!.querySelector('[data-testid="task-create-dialog"]')).not.toBeNull();
  });
});
