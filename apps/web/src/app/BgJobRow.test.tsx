// The background jobs dialog shows why a failed job ended (#4726). The registry has
// stored `background_jobs.error` since #3945 and the API returns it, but the row never
// rendered it, so a job a restart interrupted just said "failed".
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Accordion } from "@protolabsai/ui/navigation";

import type { BackgroundJobDTO } from "../lib/types";
import { BgJobRow } from "./BackgroundJobs";
import { jobFailureReason } from "./background-jobs";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const INTERRUPTED = "interrupted: the server restarted before the job finished";

function job(p: Partial<BackgroundJobDTO>): BackgroundJobDTO {
  return { id: "j1", status: "failed", subagent_type: "researcher", description: "dig", ...p };
}

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

function renderRow(j: BackgroundJobDTO) {
  const noop = () => {};
  act(() =>
    root.render(
      h(Accordion, null, h(BgJobRow, { job: j, tools: [], onStop: noop, onDelete: noop, onJumpToChat: noop })),
    ),
  );
  return container.querySelector('[data-testid="background-job-error"]');
}

describe("BgJobRow failure reason", () => {
  it("shows a failed job's stored error in the row head", () => {
    expect(renderRow(job({ error: INTERRUPTED }))?.textContent).toBe(INTERRUPTED);
  });

  it("shows nothing for a completed job or a failed job with no error", () => {
    expect(renderRow(job({ status: "completed", result: "ok", error: "leftover" }))).toBeNull();
    expect(renderRow(job({ error: "" }))).toBeNull();
  });
});

describe("jobFailureReason", () => {
  it("is the trimmed error for a failed or canceled job and empty otherwise", () => {
    expect(jobFailureReason({ status: "failed", error: ` ${INTERRUPTED} ` })).toBe(INTERRUPTED);
    expect(jobFailureReason({ status: "canceled", error: "stopped by the operator" })).toBe("stopped by the operator");
    expect(jobFailureReason({ status: "running", error: "x" })).toBe("");
    expect(jobFailureReason({ status: "completed", error: "x" })).toBe("");
    expect(jobFailureReason({ status: "failed" })).toBe("");
  });
});
