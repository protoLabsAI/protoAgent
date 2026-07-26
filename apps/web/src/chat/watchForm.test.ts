import { describe, expect, it } from "vitest";

import {
  OPERATOR_SESSION,
  WATCH_VERIFIER_TYPES,
  buildWatchCreateBody,
  parseDuration,
  watchFormPayload,
} from "./watchForm";

// The operator watch-creation form (ADR 0067). Pure logic only — the schema shape and the
// answers→POST body mapping — mirroring goalForm.test.ts, since the two forms deliberately
// share a vocabulary and would otherwise drift apart silently.

describe("watchFormPayload", () => {
  it("asks for a condition first and defaults the verifier to llm", () => {
    const p = watchFormPayload();
    const step1 = p.steps![0].schema as Record<string, any>;
    expect(p.steps).toHaveLength(2);
    expect(step1.required).toEqual(["condition"]);
    expect(step1.properties.verifier.default).toBe("llm");
    // Cards, not a dropdown: HitlForm turns a oneOf-with-descriptions into option cards.
    expect(step1.properties.verifier.oneOf).toHaveLength(WATCH_VERIFIER_TYPES.length);
  });

  it("shows only the input the picked verifier needs", () => {
    const props = watchFormPayload().steps![0].schema.properties as Record<string, any>;
    expect(props.verify_command.showWhen).toEqual({ field: "verifier", in: ["command", "test"] });
    expect(props.verify_ci.showWhen).toEqual({ field: "verifier", equals: "ci" });
    expect(props.verify_data_path.showWhen).toEqual({ field: "verifier", equals: "data" });
    // llm needs nothing — no conditional field claims it.
    const claimsLlm = Object.values(props).some((f: any) =>
      f?.showWhen?.equals === "llm" || f?.showWhen?.in?.includes("llm"),
    );
    expect(claimsLlm).toBe(false);
  });

  it("puts the reaction and the cadence knobs on the optional second step", () => {
    const step2 = watchFormPayload().steps![1].schema as Record<string, any>;
    expect(step2.required ?? []).toEqual([]);
    expect(Object.keys(step2.properties).sort()).toEqual(
      ["expires_in", "interval", "run_prompt", "stall_after"].sort(),
    );
  });
});

describe("parseDuration", () => {
  it("reads the m/h/d vocabulary the rest of the product uses", () => {
    expect(parseDuration("30m")).toBe(1800);
    expect(parseDuration("2h")).toBe(7200);
    expect(parseDuration("7d")).toBe(604800);
    expect(parseDuration(" 45s ")).toBe(45);
    expect(parseDuration("6H")).toBe(21600);
  });

  it("returns null rather than guessing", () => {
    // The caller OMITS the field on null, so a typo can't silently arm a 0-second cadence.
    for (const bad of ["", "soon", "2 weeks", "-3h", "0m", "h", null, undefined, {}]) {
      expect(parseDuration(bad as unknown)).toBeNull();
    }
  });
});

describe("buildWatchCreateBody", () => {
  const NOW = new Date(2026, 6, 1, 12, 0, 0).getTime();

  it("needs a condition", () => {
    expect(buildWatchCreateBody({}, NOW)).toBeNull();
    expect(buildWatchCreateBody({ condition: "   " }, NOW)).toBeNull();
  });

  it("is a bare llm watch when nothing optional is filled in", () => {
    expect(buildWatchCreateBody({ condition: "the deploy finishes" }, NOW)).toEqual({
      condition: "the deploy finishes",
      verifier: { type: "llm" },
    });
  });

  it("maps each verifier to its own spec shape", () => {
    const at = (a: Record<string, unknown>) => buildWatchCreateBody({ condition: "c", ...a }, NOW)!.verifier;
    expect(at({ verifier: "command", verify_command: "kubectl rollout status" })).toEqual({
      type: "command",
      command: "kubectl rollout status",
    });
    expect(at({ verifier: "test", verify_command: "pytest -q" })).toEqual({ type: "test", command: "pytest -q" });
    expect(at({ verifier: "data", verify_data_path: "/tmp/x", verify_data_contains: "ok" })).toEqual({
      type: "data",
      path: "/tmp/x",
      contains: "ok",
    });
    // A blank `contains` means "just require the file" — the key is omitted, not sent empty.
    expect(at({ verifier: "data", verify_data_path: "/tmp/x" })).toEqual({ type: "data", path: "/tmp/x" });
  });

  it("splits a ci ref into pr vs branch, tolerating a leading #", () => {
    const at = (ref: string) =>
      buildWatchCreateBody({ condition: "c", verifier: "ci", verify_ci: ref }, NOW)!.verifier;
    expect(at("#1785")).toEqual({ type: "ci", pr: "1785" });
    expect(at("1785")).toEqual({ type: "ci", pr: "1785" });
    expect(at("main")).toEqual({ type: "ci", branch: "main" });
  });

  it("turns 'give up after' into an absolute deadline", () => {
    // The form asks for a DURATION because that's what an operator knows; the API stores an
    // epoch. Same relative-in/absolute-stored split the create_watch tool uses.
    const body = buildWatchCreateBody({ condition: "c", expires_in: "6h" }, NOW)!;
    expect(body.deadline).toBe(Math.round(NOW / 1000) + 21600);
  });

  it("carries the cadence and stall knobs, omitting what wasn't given", () => {
    const body = buildWatchCreateBody({ condition: "c", interval: "30m", stall_after: 3 }, NOW)!;
    expect(body.interval_s).toBe(1800);
    expect(body.stall_after).toBe(3);
    expect(body.deadline).toBeUndefined();
    expect(body.run_prompt).toBeUndefined();
  });

  it("pairs a reaction prompt with a session, or sends neither", () => {
    // The controller drops a reaction that has no target session, so they travel together.
    const withPrompt = buildWatchCreateBody({ condition: "c", run_prompt: "Run the smoke test." }, NOW)!;
    expect(withPrompt.run_prompt).toBe("Run the smoke test.");
    expect(withPrompt.run_session).toBe(OPERATOR_SESSION);

    const without = buildWatchCreateBody({ condition: "c", run_prompt: "   " }, NOW)!;
    expect(without.run_prompt).toBeUndefined();
    expect(without.run_session).toBeUndefined();
  });

  it("ignores a junk cadence instead of arming a broken watch", () => {
    const body = buildWatchCreateBody({ condition: "c", interval: "soon", expires_in: "later" }, NOW)!;
    expect(body.interval_s).toBeUndefined();
    expect(body.deadline).toBeUndefined();
  });
});
