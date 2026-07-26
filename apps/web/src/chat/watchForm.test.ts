import { describe, expect, it } from "vitest";

import {
  OPERATOR_SESSION,
  FALLBACK_VERIFIER_TYPES,
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
    expect(step1.properties.verifier.oneOf).toHaveLength(FALLBACK_VERIFIER_TYPES.types.length);
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


describe("verifier options come from the catalog, not a hardcoded list", () => {
  const CATALOG = {
    types: [
      { value: "command", description: "A shell command that exits 0", source: "core" as const },
      { value: "llm", description: "Fuzzy LLM judgment (the default)", source: "core" as const },
      { value: "plugin", description: "A check contributed by an installed plugin", source: "core" as const },
    ],
    plugin_checks: [
      { name: "spacetraders:credits", plugin_id: "spacetraders", description: "Credits at or above args.min", source: "plugin" as const },
      { name: "careercoach:new_matches", plugin_id: "careercoach", description: "", source: "plugin" as const },
    ],
  };
  const verifierField = (c?: typeof CATALOG) =>
    (watchFormPayload(c).steps![0].schema as any).properties.verifier;
  const props = (c?: typeof CATALOG) => (watchFormPayload(c).steps![0].schema as any).properties;

  it("renders whatever the server says, including plugin", () => {
    expect(verifierField(CATALOG).oneOf.map((o: any) => o.const)).toEqual(["command", "llm", "plugin"]);
  });

  it("hides the plugin type when nothing registers a check", () => {
    // A `plugin` card whose picker would be empty is worse than no card at all.
    const bare = { ...CATALOG, plugin_checks: [] };
    expect(verifierField(bare).oneOf.map((o: any) => o.const)).toEqual(["command", "llm"]);
  });

  it("lists the registered checks, falling back to the plugin id for an undescribed one", () => {
    const picker = props(CATALOG).verify_plugin_check;
    expect(picker.showWhen).toEqual({ field: "verifier", equals: "plugin" });
    expect(picker.oneOf.map((o: any) => [o.const, o.description])).toEqual([
      ["spacetraders:credits", "Credits at or above args.min"],
      // Registered before `description` existed → attributed by plugin instead of blank.
      ["careercoach:new_matches", "from careercoach"],
    ]);
  });

  it("falls back to the core types when the catalog never loaded", () => {
    // The fetch can fail; the form must still be usable, just without the plugin class.
    expect(verifierField().oneOf.map((o: any) => o.const)).toEqual(
      FALLBACK_VERIFIER_TYPES.types.map((v) => v.value),
    );
    expect(verifierField().oneOf.map((o: any) => o.const)).not.toContain("plugin");
  });
});

describe("buildWatchCreateBody — plugin verifier", () => {
  const NOW = 1_800_000_000_000;
  const verifierOf = (a: Record<string, unknown>) =>
    buildWatchCreateBody({ condition: "c", verifier: "plugin", ...a }, NOW)!.verifier;

  it("carries the check and parsed args", () => {
    expect(
      verifierOf({ verify_plugin_check: "spacetraders:credits", verify_plugin_args: '{"min": 1000000}' }),
    ).toEqual({ type: "plugin", check: "spacetraders:credits", args: { min: 1000000 } });
  });

  it("omits args entirely when absent or unparseable", () => {
    // We can't know each plugin's arg schema, so bad JSON is dropped and the verifier says
    // what it needed — better than sending a string it can't read.
    expect(verifierOf({ verify_plugin_check: "x:y" })).toEqual({ type: "plugin", check: "x:y" });
    expect(verifierOf({ verify_plugin_check: "x:y", verify_plugin_args: "min: 5" })).toEqual({
      type: "plugin",
      check: "x:y",
    });
    // A bare array/scalar isn't an args object either.
    expect(verifierOf({ verify_plugin_check: "x:y", verify_plugin_args: "[1,2]" })).toEqual({
      type: "plugin",
      check: "x:y",
    });
  });
});
