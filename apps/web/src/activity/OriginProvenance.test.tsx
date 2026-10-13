// DS audit card 4: the activity feed's local `function Badge` (which shadowed the DS
// Badge and was never a badge — it renders the provenance row) was renamed
// `OriginProvenance`. Pure rename, so this pins the rendered markup (origin chip,
// trigger, priority, relative time) is unchanged under the new name, and guards that no
// local `Badge` component sneaks back into the file.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ActivityEntry } from "../lib/types";
import { OriginProvenance } from "./ActivitySurface";
import activitySrc from "./ActivitySurface.tsx?raw";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ENTRY: ActivityEntry = {
  id: 1,
  created_at: "2020-01-01T00:00:00Z",
  origin: "scheduler",
  trigger: "cron:nightly",
  priority: "now",
  state: "completed",
  text: "done",
  task_id: "",
  stimulus: "",
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
});

describe("OriginProvenance (renamed from the local Badge)", () => {
  it("renders the provenance row: origin chip, trigger, priority, relative time", () => {
    act(() => root.render(h(OriginProvenance, { entry: ENTRY })));

    const prov = container.querySelector(".activity-prov");
    expect(prov).toBeTruthy();
    // origin → mapped label + per-origin class
    const origin = prov!.querySelector(".activity-origin-scheduler");
    expect(origin?.textContent).toContain("scheduled");
    expect(prov!.querySelector(".activity-trigger")?.textContent).toBe("cron:nightly");
    expect(prov!.querySelector(".inbox-pri-now")?.textContent).toBe("now");
    expect(prov!.querySelector(".activity-time")).toBeTruthy();
  });

  it("no local component named Badge remains in the file", () => {
    expect(activitySrc).not.toMatch(/function Badge\b/);
    expect(activitySrc).not.toMatch(/<Badge[\s/>]/);
    expect(activitySrc).toMatch(/function OriginProvenance\b/);
  });
});
