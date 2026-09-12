import { describe, expect, it } from "vitest";

import { jobLabel } from "./BackgroundWorkStrip";
import { runningJobsFor, type JobLite } from "./backgroundJobStore";
import { briefSummary, delegationState } from "./delegation";
import { liveServerTurnMessageId } from "./ChatTranscript";
import type { ChatMessage } from "../lib/types";

describe("delegationState — what the delegation row shows", () => {
  it("tracks a background delegation by its job", () => {
    const d = { background: true, jobId: "bg-4109c71161eb" };
    expect(delegationState(d, "running")).toBe("running");
    expect(delegationState(d, "completed")).toBe("done");
    expect(delegationState(d, "failed")).toBe("failed");
    expect(delegationState(d, "canceled")).toBe("failed");
  });

  it("never spins for a job it doesn't know (pruned, or not loaded yet)", () => {
    expect(delegationState({ background: true, jobId: "bg-000000000000" }, undefined)).toBe("sent");
  });

  it("a foreground delegation is just sent — the delegate's reply follows as its own message", () => {
    expect(delegationState(undefined, undefined)).toBe("sent");
    expect(delegationState({ summary: "look at auth" }, "running")).toBe("sent");
  });

  it("a dispatch that errored is failed, whatever else it says", () => {
    expect(delegationState({ background: true, error: "Error: unknown delegate" }, undefined)).toBe("failed");
  });
});

describe("briefSummary — the fallback when an ask carries no summary", () => {
  it("takes the prompt's first sentence", () => {
    expect(briefSummary("Repo: protoLabsAI/x. Land PR #13, then close #12.")).toBe("Repo: protoLabsAI/x.");
  });
  it("or its first line", () => {
    expect(briefSummary("Review the auth module\nThen report back.")).toBe("Review the auth module");
  });
  it("clips a long one", () => {
    const s = briefSummary("word ".repeat(80), 40);
    expect(s.length).toBeLessThanOrEqual(40);
    expect(s.endsWith("…")).toBe(true);
  });
});

const job = (over: Partial<JobLite>): JobLite => ({
  id: "bg-1",
  status: "running",
  subagent_type: "delegate",
  description: "delegate → sonnet: Land PR #13",
  origin_session: "s1",
  ...over,
});

describe("background work in a chat", () => {
  it("counts only this session's RUNNING jobs", () => {
    const all = {
      a: job({ id: "a" }),
      b: job({ id: "b", status: "completed" }),
      c: job({ id: "c", origin_session: "s2" }),
      d: job({ id: "d", subagent_type: "researcher", description: "Survey the codebase" }),
    };
    expect(runningJobsFor(all, "s1").map((j) => j.id)).toEqual(["a", "d"]);
  });

  it("labels a delegation by its delegate and summary", () => {
    expect(jobLabel(job({}))).toBe("sonnet: Land PR #13");
    expect(jobLabel(job({ subagent_type: "researcher", description: "Survey the codebase" }))).toBe(
      "Survey the codebase",
    );
    expect(jobLabel(job({ description: "" }))).toBe("delegate");
  });
});

describe("one activity cue per server-fired turn", () => {
  const msg = (over: Partial<ChatMessage>): ChatMessage => ({ id: "m", role: "assistant", content: "x", ...over });

  it("names the live message the turn is streaming into", () => {
    const messages = [msg({ id: "u", role: "user" }), msg({ id: "live", status: "streaming" })];
    expect(liveServerTurnMessageId(messages, "responding to background reports…")).toBe("live");
  });

  it("has none before the turn's first frame, or when no server turn is running", () => {
    const settled = [msg({ id: "done", status: "done" })];
    expect(liveServerTurnMessageId(settled, "responding to background reports…")).toBeNull();
    expect(liveServerTurnMessageId([msg({ id: "live", status: "streaming" })], null)).toBeNull();
  });
});
