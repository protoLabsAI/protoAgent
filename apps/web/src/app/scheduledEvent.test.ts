// Unit coverage for the scheduler.completed → chat-message mapping (#2990). Pure, so it
// needs no DOM/React mount — it proves the event carries into the ScheduledReportCard /
// ScheduledChip payload correctly, including the recurring-collapse flag and the guards.
import { describe, expect, it } from "vitest";

import { parseScheduledEvent } from "./scheduledEvent";

describe("parseScheduledEvent (#2990)", () => {
  it("maps a completed cron fire into a session + full-card payload (r2)", () => {
    const p = parseScheduledEvent({
      job_id: "j1",
      origin_session: "chat-42",
      fired_at: "2026-08-22T14:00:00Z",
      summary: "Swept the inbox: 3 new threads.",
      status: "completed",
      collapse: false,
      activity_context: "system:activity",
      task_id: "t1",
    });
    expect(p).not.toBeNull();
    expect(p!.session).toBe("chat-42");
    expect(p!.scheduled.jobId).toBe("j1");
    expect(p!.scheduled.firedAt).toBe("2026-08-22T14:00:00Z");
    expect(p!.scheduled.summary).toBe("Swept the inbox: 3 new threads.");
    expect(p!.scheduled.status).toBe("completed");
    expect(p!.scheduled.collapse).toBe(false); // first fire → full card
    expect(p!.scheduled.activityContext).toBe("system:activity");
  });

  it("returns null without a job id or an origin session (r6 — backward compatible)", () => {
    expect(parseScheduledEvent({ origin_session: "chat-42" })).toBeNull();
    expect(parseScheduledEvent({ job_id: "j1" })).toBeNull();
    expect(parseScheduledEvent({})).toBeNull();
  });

  it("flags a recurring re-fire so the console renders the compact chip (r4)", () => {
    const p = parseScheduledEvent({ job_id: "j1", origin_session: "chat-42", collapse: true });
    expect(p!.scheduled.collapse).toBe(true);
  });

  it("coerces an unknown status to completed and preserves a real failure", () => {
    expect(parseScheduledEvent({ job_id: "j1", origin_session: "c", status: "weird" })!.scheduled.status).toBe(
      "completed",
    );
    expect(parseScheduledEvent({ job_id: "j1", origin_session: "c", status: "failed" })!.scheduled.status).toBe(
      "failed",
    );
  });
});
